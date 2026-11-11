"""Live view: watch a local folder and stream its graph changes to the browser as they happen.

`graph_repo.py live <folder>` starts a small web server on 127.0.0.1 that serves the graph view and
pushes every change to it over Server-Sent Events. A background thread polls the folder (size and
modification time of the files the graph shows, plus git's HEAD); when something changes it waits for
the writes to settle, rebuilds the graph (only the changed files are parsed again, see parsecache.py)
and sends the difference. The view's Live tab lists the changes as they arrive and marks them on the
overview and the map.

Claude Code's PostToolUse hook (hooks/hooks.json → scripts/live_notify.py) posts the path of every
file Claude edits to /api/notify. Those changes are labelled with the tool that made them, and they show
up without waiting for the next poll. Only this machine can reach the server: it listens on 127.0.0.1,
answers only requests addressed to localhost, and /api/notify and /api/stop need the token from the
lock file, which only the user's own account can read.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import secrets
import threading
import time
import urllib.request
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from .classify import classify, is_ignored_dir
from .graph import CodeGraph
from .history import describe_counts, diff_graphs

log = logging.getLogger("repo_graph_mcp")

DEFAULT_PORT = 7333
MAX_EVENTS = 200
ATTRIBUTION_WINDOW = 20.0  # seconds: a hook notice this recent explains a change to that file
SETTLE = 0.3  # wait this long for a burst of writes to finish before rebuilding
SETTLE_MAX = 2.0
CONFIG_FILES = {"tsconfig.json", "jsconfig.json"}  # read by the builder for import paths

Rebuild = Callable[[], "tuple[CodeGraph, dict[str, Any] | None]"]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def norm(path: str | os.PathLike) -> str:
    """One spelling per file, so a path from the hook matches the one being watched (case on Windows)."""
    return os.path.normcase(os.path.realpath(os.fspath(path)))


# ── lock files: how the hook and a second `live` call find a running server ──────────

def lock_dir(home: Path) -> Path:
    return home / "live"


def lock_path(home: Path, root: Path) -> Path:
    return lock_dir(home) / f"{hashlib.sha1(norm(root).encode('utf8')).hexdigest()[:12]}.json"


def _local_opener() -> urllib.request.OpenerDirector:
    # never send localhost traffic through an HTTP(S)_PROXY from the environment
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def find_running(home: Path, root: Path) -> dict[str, Any] | None:
    """The lock of a live server already watching `root`, if one answers."""
    p = lock_path(home, root)
    try:
        lock = json.loads(p.read_text("utf8"))
    except Exception:
        return None
    try:
        with _local_opener().open(f"http://127.0.0.1:{int(lock['port'])}/api/ping", timeout=1.0) as r:
            info = json.loads(r.read().decode("utf8"))
        if norm(info.get("root", "")) == norm(root):
            return lock
    except Exception:
        pass
    try:  # nobody answers: it was left behind by a server that didn't exit cleanly
        p.unlink()
    except OSError:
        pass
    return None


def stop_running(lock: dict[str, Any]) -> bool:
    req = urllib.request.Request(f"http://127.0.0.1:{int(lock['port'])}/api/stop", data=b"{}", method="POST",
                                 headers={"X-Repo-Graph-Token": lock.get("token", ""), "Content-Type": "application/json"})
    try:
        _local_opener().open(req, timeout=2.0).close()
        return True
    except Exception:
        return False


# ── watching the folder ──────────────────────────────────────────────────────

class Scanner:
    """Size and mtime of every file the graph shows (the builder's own rules), and git's HEAD."""

    def __init__(self, root: Path):
        self.root = root
        self.git_dir = _git_dir(root)

    def snapshot(self) -> dict[str, tuple[int, int]]:
        out: dict[str, tuple[int, int]] = {}
        root = str(self.root)
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not is_ignored_dir(d)]
            rel_dir = os.path.relpath(dirpath, root).replace(os.sep, "/")
            rel_dir = "" if rel_dir == "." else rel_dir
            for fn in filenames:
                rel = f"{rel_dir}/{fn}" if rel_dir else fn
                if fn not in CONFIG_FILES and classify(rel) is None:
                    continue
                try:
                    st = os.stat(os.path.join(dirpath, fn))
                except OSError:
                    continue
                out[rel] = (st.st_mtime_ns, st.st_size)
        return out

    def git_sig(self) -> tuple:
        """Changes when HEAD moves: a commit, checkout, reset, merge or pull."""
        if not self.git_dir:
            return ()
        sig: list[Any] = []
        for name in ("HEAD", os.path.join("logs", "HEAD")):
            try:
                st = os.stat(os.path.join(self.git_dir, name))
                sig.append((st.st_mtime_ns, st.st_size))
            except OSError:
                sig.append(None)
        return tuple(sig)

    @staticmethod
    def changes(old: dict[str, tuple[int, int]], new: dict[str, tuple[int, int]]) -> list[dict[str, str]]:
        out = [{"path": p, "status": "added"} for p in new if p not in old]
        out += [{"path": p, "status": "modified"} for p in new if p in old and old[p] != new[p]]
        out += [{"path": p, "status": "deleted"} for p in old if p not in new]
        out.sort(key=lambda f: f["path"])
        return out


def _git_dir(root: Path) -> str | None:
    import shutil
    import subprocess

    if not shutil.which("git"):
        return None
    try:
        p = subprocess.run(["git", "-C", str(root), "rev-parse", "--absolute-git-dir"], capture_output=True,
                           text=True, timeout=10)
        return p.stdout.strip() or None if p.returncode == 0 else None
    except Exception:
        return None


def _title(files: list[dict[str, str]]) -> str:
    if not files:
        return "No file changes"
    if len(files) == 1:
        f = files[0]
        verb = {"added": "Created", "deleted": "Deleted"}.get(f["status"], "Edited")
        return f"{verb} {f['path'].rsplit('/', 1)[-1]}"
    return f"{len(files)} files changed"


def _has_changes(d: dict[str, Any] | None) -> bool:
    return bool(d and (d["added"] or d["changed"] or d["removed"]))


# ── one watched folder ───────────────────────────────────────────────────────

class LiveSession:
    """The current graph of a folder, the changes since watching began, and the people watching."""

    def __init__(self, root: Path, rebuild: Rebuild, *, interval: float = 1.0, max_nodes: int = 6000,
                 save: Callable[[], None] | None = None):
        self.root = Path(os.path.realpath(root))
        self.rebuild = rebuild
        self.interval = max(0.2, interval)
        self.max_nodes = max_nodes
        self.save = save
        self.lock = threading.RLock()
        self.scanner = Scanner(self.root)
        self.started = now_iso()
        self.events: deque[dict[str, Any]] = deque(maxlen=MAX_EVENTS)
        self.seq = 0  # events
        self.graph_seq = 0  # graph versions
        self.notes: dict[str, tuple[float, str]] = {}  # normalised path -> (time, tool) from the hook
        self.hook_seen: str | None = None
        self.building = False
        self.error: str | None = None
        self.wake = threading.Event()
        self.stopped = threading.Event()
        self.subscribers: list[queue.Queue] = []
        self.last_viewer = time.monotonic()
        self._payload: bytes | None = None
        self._since: dict[str, Any] | None = None
        self._unsaved = False
        self._last_save = time.monotonic()

        self.files = self.start_files = self.scanner.snapshot()
        self.git_state = self.scanner.git_sig()
        self.graph, self.history = rebuild()
        self.start_graph = self.graph

    # ── what the browser asks for ──
    def payload_bytes(self, graph_id: str) -> bytes:
        with self.lock:
            if self._payload is None:
                p = self.graph.to_ui_payload(max_nodes=self.max_nodes)
                p.update(graphId=graph_id, mode="repo")
                if self.history:
                    p["history"] = self.history
                p["live"] = {"root": str(self.root), "name": self.root.name, "started": self.started,
                             "seq": self.graph_seq, "interval": self.interval}
                self._payload = json.dumps(p, separators=(",", ":")).encode("utf8")
            return self._payload

    def since(self) -> dict[str, Any]:
        """Everything that changed since watching began, as one entry."""
        with self.lock:
            if self._since is None:
                d = diff_graphs(self.start_graph, self.graph)
                files = Scanner.changes(self.start_files, self.files)
                by_claude = sum(1 for e in self.events if e["source"] == "claude")
                self._since = {
                    "id": "live-since", "kind": "live", "title": "Since you started watching",
                    "label": "this live session", "files": files[:200], "diff": d,
                    "summary": describe_counts(d) if _has_changes(d) else ("no graph changes yet" if not files else "no graph changes"),
                    "changes": len(self.events), "by_claude": by_claude, "at": self.started,
                }
            return self._since

    def hello(self) -> dict[str, Any]:
        with self.lock:
            return {"root": str(self.root), "name": self.root.name, "started": self.started, "seq": self.graph_seq,
                    "events": list(self.events)[::-1][:100], "since": self.since(), "hook_seen": self.hook_seen,
                    "building": self.building, "error": self.error, "interval": self.interval}

    # ── the hook ──
    def notify(self, file: str, tool: str) -> None:
        if not file:
            return
        key = norm(file)
        now = time.time()
        with self.lock:
            self.hook_seen = now_iso()
            self.notes[key] = (now, tool or "Edit")
            # the poll may have caught this edit before the hook reported it: relabel that change
            root = norm(self.root)
            rel = key[len(root) + 1:].replace(os.sep, "/") if key.startswith(root + os.sep) else None
            updated = None
            for ev in list(self.events)[-5:]:
                if ev["source"] == "claude" or now - ev["t"] > 8 or rel is None:
                    continue
                if any(norm(self.root / f["path"]) == key for f in ev["files"]):
                    ev["source"] = "claude"
                    ev["tools"] = sorted({*ev.get("tools", []), tool or "Edit"})
                    updated = ev
            if updated:
                self._since = None
        if updated:
            self.broadcast("event", {"event": updated, "since": self.since()})
        self.broadcast("hook", {"hook_seen": self.hook_seen})
        self.wake.set()  # look now rather than at the next poll

    # ── the watcher ──
    def run(self) -> None:
        while not self.stopped.is_set():
            self.wake.wait(self.interval)
            self.wake.clear()
            if self.stopped.is_set():
                break
            try:
                self.tick()
            except Exception as e:  # keep watching whatever happens to one rebuild
                log.exception("live rebuild failed")
                with self.lock:
                    self.building = False
                    self.error = f"{type(e).__name__}: {e}"[:300]
                self.broadcast("status", {"building": False, "error": self.error})
            if self._unsaved and self.save and time.monotonic() - self._last_save > 30:
                self.flush()

    def flush(self) -> None:
        if self.save and self._unsaved:
            try:
                self.save()
            except Exception as e:
                log.warning("couldn't save the parse cache: %s", e)
            self._unsaved = False
            self._last_save = time.monotonic()

    def tick(self) -> dict[str, Any] | None:
        """Look for changes; when there are some, rebuild and tell everyone. Returns the new event."""
        files, git_state = self.scanner.snapshot(), self.scanner.git_sig()
        if files == self.files and git_state == self.git_state:
            return None
        with self.lock:
            self.building = True
        self.broadcast("status", {"building": True, "error": None})
        deadline = time.monotonic() + SETTLE_MAX
        while time.monotonic() < deadline:  # let a burst of writes finish
            time.sleep(SETTLE)
            f2, g2 = self.scanner.snapshot(), self.scanner.git_sig()
            if f2 == files and g2 == git_state:
                break
            files, git_state = f2, g2
        t0 = time.perf_counter()
        graph, hist = self.rebuild()
        ms = int((time.perf_counter() - t0) * 1000)
        self._unsaved = True
        with self.lock:
            changed = Scanner.changes(self.files, files)
            d = diff_graphs(self.graph, graph)
            head_moved = bool(hist and self.history and hist.get("head") != self.history.get("head"))
            event = self._event(changed, d, hist if head_moved else None, ms)
            self.graph, self.history, self.files, self.git_state = graph, hist, files, git_state
            self.graph_seq += 1
            self.building = False
            self.error = None
            self._payload = self._since = None
            if event:
                self.events.append(event)
            msg = {"event": event, "since": self.since(), "seq": self.graph_seq}
        self.broadcast("change", msg)
        return event

    def _event(self, files: list[dict[str, str]], d: dict[str, Any], hist: dict[str, Any] | None,
               ms: int) -> dict[str, Any] | None:
        commit = next((e for e in hist["entries"] if e["kind"] == "commit"), None) if hist else None
        if not files and not _has_changes(d) and not commit:
            return None
        now = time.time()
        tools = []
        for f in files:
            hit = self.notes.get(norm(self.root / f["path"]))
            if hit and now - hit[0] < ATTRIBUTION_WINDOW:
                tools.append(hit[1])
        self.notes = {k: v for k, v in self.notes.items() if now - v[0] < ATTRIBUTION_WINDOW}
        self.seq += 1
        ev: dict[str, Any] = {
            "id": f"live-{self.seq}", "kind": "live", "seq": self.seq, "at": now_iso(), "t": now,
            "source": "claude" if tools else "disk", "tools": sorted(set(tools)), "files": files[:200],
            "title": _title(files), "diff": d,
            "summary": describe_counts(d) if _has_changes(d) else "no graph changes", "ms": ms,
        }
        if commit and not files:  # HEAD moved and nothing on disk changed: a commit (or reset) was made
            ev.update(source="git", title=f"Committed {commit['short']} · {commit['title']}", sha=commit["sha"],
                      short=commit["short"], files=commit["files"], diff=commit["diff"] or d,
                      summary=commit["summary"], **({"url": commit["url"]} if commit.get("url") else {}))
        elif commit:
            ev["head"] = commit["short"]
        return ev

    # ── people watching ──
    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=500)
        with self.lock:
            self.subscribers.append(q)
            self.last_viewer = time.monotonic()
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self.lock:
            if q in self.subscribers:
                self.subscribers.remove(q)
            self.last_viewer = time.monotonic()

    def broadcast(self, name: str, data: dict[str, Any]) -> None:
        msg = (name, json.dumps(data, separators=(",", ":")))
        with self.lock:
            for q in list(self.subscribers):
                try:
                    q.put_nowait(msg)
                except queue.Full:  # a stalled tab: drop it; it reconnects and gets a fresh hello
                    self.subscribers.remove(q)

    def idle_for(self) -> float:
        with self.lock:
            return 0.0 if self.subscribers else time.monotonic() - self.last_viewer

    def stop(self) -> None:
        self.stopped.set()
        self.wake.set()
        with self.lock:
            for q in self.subscribers:
                try:
                    q.put_nowait(None)
                except queue.Full:
                    pass
        self.flush()


# ── the web server ───────────────────────────────────────────────────────────

class LiveServer:
    def __init__(self, session: LiveSession, page: Callable[[bytes], str], *, graph_id: str,
                 port: int = DEFAULT_PORT, home: Path | None = None, idle_minutes: float = 0):
        self.session = session
        self.page = page
        self.graph_id = graph_id
        self.home = home
        self.idle_minutes = idle_minutes
        self.token = secrets.token_urlsafe(18)
        handler = _make_handler(self)
        try:
            self.httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
        except OSError:  # taken: let the OS pick a free one
            self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/"
        self.hosts = {f"127.0.0.1:{self.port}", f"localhost:{self.port}", f"[::1]:{self.port}"}
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        """Serve in background threads (tests, embedding). `serve_forever` blocks instead."""
        self._write_lock()
        for target in (self.session.run, self._watch_idle, self.httpd.serve_forever):
            t = threading.Thread(target=target, daemon=True)
            t.start()
            self._threads.append(t)

    def serve_forever(self) -> None:
        self._write_lock()
        for target in (self.session.run, self._watch_idle):
            t = threading.Thread(target=target, daemon=True)
            t.start()
            self._threads.append(t)
        try:
            self.httpd.serve_forever(poll_interval=0.5)
        except KeyboardInterrupt:
            pass
        finally:
            self.close()

    def shutdown(self) -> None:
        threading.Thread(target=self.httpd.shutdown, daemon=True).start()

    def close(self) -> None:
        self.session.stop()
        self._remove_lock()
        try:
            self.httpd.server_close()
        except Exception:
            pass

    def _watch_idle(self) -> None:
        if not self.idle_minutes:
            return
        while not self.session.stopped.wait(15):
            if self.session.idle_for() > self.idle_minutes * 60:
                log.info("no one has watched for %s minutes: stopping", self.idle_minutes)
                self.shutdown()
                return

    def _write_lock(self) -> None:
        if not self.home:
            return
        p = lock_path(self.home, self.session.root)
        p.parent.mkdir(parents=True, exist_ok=True)
        data = {"root": str(self.session.root), "port": self.port, "pid": os.getpid(), "token": self.token,
                "url": self.url, "started": self.session.started}
        fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # the token is for this user only
        with os.fdopen(fd, "w", encoding="utf8") as fh:
            json.dump(data, fh)

    def _remove_lock(self) -> None:
        if not self.home:
            return
        p = lock_path(self.home, self.session.root)
        try:
            if json.loads(p.read_text("utf8")).get("pid") == os.getpid():
                p.unlink()
        except Exception:
            pass


def _make_handler(server: LiveServer) -> type[BaseHTTPRequestHandler]:
    s = server.session

    class Handler(BaseHTTPRequestHandler):
        server_version = "repo-graph-live"

        def log_message(self, fmt: str, *args: Any) -> None:  # quiet: the terminal is the user's
            log.debug("live: " + fmt, *args)

        def _host_ok(self) -> bool:
            # a page on another site could point a DNS name at 127.0.0.1; only answer requests meant for us
            if self.headers.get("Host", "") in server.hosts:
                return True
            self._send(421, b"Misdirected request", "text/plain")
            return False

        def _send(self, code: int, body: bytes, ctype: str, extra: dict[str, str] | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, data: Any, code: int = 200) -> None:
            self._send(code, json.dumps(data, separators=(",", ":")).encode("utf8"), "application/json")

        def do_GET(self) -> None:  # noqa: N802
            if not self._host_ok():
                return
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                html = server.page(s.payload_bytes(server.graph_id))
                self._send(200, html.encode("utf8"), "text/html; charset=utf-8",
                           {"Content-Security-Policy": "default-src 'self'; script-src 'unsafe-inline'; "
                                                       "style-src 'unsafe-inline'; img-src 'self' data:; "
                                                       "connect-src 'self'; frame-ancestors 'self'"})
            elif path == "/api/graph":
                self._send(200, s.payload_bytes(server.graph_id), "application/json")
            elif path == "/api/state":
                self._json(s.hello())
            elif path == "/api/ping":
                self._json({"root": str(s.root), "started": s.started})
            elif path == "/api/events":
                self._events()
            else:
                self._send(404, b"Not found", "text/plain")

        def do_POST(self) -> None:  # noqa: N802
            if not self._host_ok():
                return
            if not secrets.compare_digest(self.headers.get("X-Repo-Graph-Token", ""), server.token):
                self._send(403, b"Forbidden", "text/plain")
                return
            try:
                n = min(int(self.headers.get("Content-Length") or 0), 64 * 1024)
                body = json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                body = {}
            path = self.path.split("?", 1)[0]
            if path == "/api/notify":
                s.notify(str(body.get("file") or ""), str(body.get("tool") or ""))
                self._send(204, b"", "text/plain")
            elif path == "/api/stop":
                self._send(204, b"", "text/plain")
                server.shutdown()
            else:
                self._send(404, b"Not found", "text/plain")

        def _events(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            q = s.subscribe()
            try:
                self._emit("hello", json.dumps(s.hello(), separators=(",", ":")))
                while not s.stopped.is_set():
                    try:
                        msg = q.get(timeout=15)
                    except queue.Empty:
                        self.wfile.write(b": ping\n\n")  # keeps proxies and the browser from timing out
                        self.wfile.flush()
                        continue
                    if msg is None:
                        break
                    self._emit(*msg)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                pass
            finally:
                s.unsubscribe(q)

        def _emit(self, name: str, data: str) -> None:
            self.wfile.write(f"event: {name}\ndata: {data}\n\n".encode("utf8"))
            self.wfile.flush()

    return Handler
