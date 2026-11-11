"""Run the watched repo's own frontend, so the live view's UI tab can show it.

`detect()` looks for the frontend: the shallowest package.json with a `dev` (or `start`/`serve`) script
whose dependencies include a web framework (Vite, Next.js, Create React App, Vue, Nuxt, Svelte(Kit),
Astro, Angular, Remix, Gatsby, Parcel, webpack-dev-server). The package manager follows the lock file.
Without one, a folder with an `index.html` is served as a static site on a second local port.
`--ui-cmd`, `--ui-dir` and `--ui-url` override all of it (e.g. `streamlit run app.py`).

The dev server is started only when the user asks (the UI tab's Start button, or `live --ui`). Its output
is kept (last 400 lines) and its local URL is read from it ("Local: http://localhost:5173/"). Stopping the
live view stops it, with everything it started.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from collections import deque
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from .classify import is_ignored_dir

FRAMEWORKS = [  # dependency -> name shown to the user, in order of preference
    ("next", "Next.js"), ("nuxt", "Nuxt"), ("@sveltejs/kit", "SvelteKit"), ("astro", "Astro"),
    ("@remix-run/dev", "Remix"), ("gatsby", "Gatsby"), ("@angular/core", "Angular"), ("vite", "Vite"),
    ("react-scripts", "Create React App"), ("@vue/cli-service", "Vue CLI"), ("parcel", "Parcel"),
    ("webpack-dev-server", "webpack"), ("svelte", "Svelte"), ("react", "React"), ("vue", "Vue"),
]
SCRIPTS = ("dev", "start", "serve", "develop")
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")
URL = re.compile(r"https?://(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\]|\[::\]):\d{2,5}(?:/[^\s'\"()<>\[\],]*)?")
STATIC_DIRS = ("", "public", "frontend", "web", "site", "static", "www", "app", "client")


def _pm(folder: Path) -> str:
    for lock, pm in (("pnpm-lock.yaml", "pnpm"), ("yarn.lock", "yarn"), ("bun.lockb", "bun"), ("bun.lock", "bun")):
        for d in (folder, *folder.parents):
            if (d / lock).exists():
                return pm
            if (d / ".git").exists():
                break
    return "npm"


def detect(root: Path) -> dict[str, Any] | None:
    """The repo's frontend and how to run it, or None."""
    best: tuple[tuple, dict[str, Any]] | None = None
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        depth = 0 if rel_dir == "." else rel_dir.count(os.sep) + 1
        dirnames[:] = [] if depth >= 3 else [d for d in dirnames if not is_ignored_dir(d)]
        if "package.json" not in filenames:
            continue
        try:
            pkg = json.loads(Path(dirpath, "package.json").read_text("utf8"))
        except Exception:
            continue
        scripts = pkg.get("scripts") or {}
        script = next((s for s in SCRIPTS if s in scripts), None)
        deps = {**(pkg.get("devDependencies") or {}), **(pkg.get("dependencies") or {})}
        rank = next((i for i, (dep, _) in enumerate(FRAMEWORKS) if dep in deps), None)
        if not script or rank is None:
            continue
        folder = Path(dirpath)
        pm = _pm(folder)
        info = {"kind": "dev", "dir": "" if rel_dir == "." else rel_dir.replace(os.sep, "/"), "script": script,
                "framework": FRAMEWORKS[rank][1], "pm": pm, "cmd": f"{pm} run {script}" if pm != "yarn" else f"yarn {script}",
                "needs_install": not (folder / "node_modules").exists()}
        key = (depth, rank, SCRIPTS.index(script))
        if best is None or key < best[0]:
            best = (key, info)
    if best:
        return best[1]
    for d in STATIC_DIRS:
        if (root / d / "index.html").is_file():
            return {"kind": "static", "dir": d, "framework": "Static site", "cmd": "", "needs_install": False}
    return None


class _Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *a: Any) -> None:
        pass

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


class AppRunner:
    def __init__(self, root: Path, *, cmd: str | None = None, cwd: str | None = None, url: str | None = None,
                 on_change: Callable[[dict[str, Any]], None] | None = None):
        self.root = Path(root)
        self.on_change = on_change
        self.lock = threading.RLock()
        self.log: deque[str] = deque(maxlen=400)
        self.proc: subprocess.Popen | None = None
        self.static: ThreadingHTTPServer | None = None
        self.state = "idle"  # idle | installing | starting | running | exited | failed
        self.url: str | None = url
        self.exit_code: int | None = None
        self.error: str | None = None
        self.fixed_url = url
        if cmd:
            self.info: dict[str, Any] | None = {"kind": "custom", "dir": cwd or "", "framework": "Your command",
                                                "cmd": cmd, "needs_install": False}
        elif url and not cwd:
            self.info = {"kind": "url", "dir": "", "framework": "Running app", "cmd": "", "needs_install": False}
        else:
            self.info = detect(self.root / cwd if cwd else self.root)
            if self.info and cwd:
                self.info["dir"] = "/".join(x for x in (cwd.strip("/\\").replace("\\", "/"), self.info["dir"]) if x)
        if self.info and self.info["kind"] == "url":
            self.state = "running"

    # ── what the UI shows ──
    def status(self) -> dict[str, Any]:
        with self.lock:
            return {"state": self.state, "url": self.url, "exit_code": self.exit_code, "error": self.error,
                    "app": self.info, "log": list(self.log)[-200:]}

    def _changed(self) -> None:
        if self.on_change:
            try:
                self.on_change(self.status())
            except Exception:
                pass

    def _say(self, line: str) -> None:
        with self.lock:
            self.log.append(line)

    # ── running ──
    def start(self) -> None:
        with self.lock:
            if not self.info or self.state in ("starting", "running", "installing"):
                return
            info = self.info
            if info["kind"] == "static":
                return self._start_static()
            if info.get("needs_install"):
                return self._run([*self._pm_cmd(info["pm"]), "install"], then_start=True)
            if info["kind"] == "custom":
                self._spawn(info["cmd"], shell=True)
            else:
                self._spawn(self._script_cmd(info))

    def install(self) -> None:
        with self.lock:
            if self.info and self.info.get("pm") and self.state not in ("installing",):
                self._run([*self._pm_cmd(self.info["pm"]), "install"], then_start=False)

    def stop(self) -> None:
        with self.lock:
            proc, self.proc = self.proc, None
            static, self.static = self.static, None
            if proc or static:
                self.state = "idle"
                self.url = self.fixed_url
        if proc and proc.poll() is None:
            _kill_tree(proc)
        if static:
            threading.Thread(target=static.shutdown, daemon=True).start()
        self._changed()

    def _pm_cmd(self, pm: str) -> list[str]:
        exe = shutil.which(pm) or pm
        return ["cmd", "/c", exe] if os.name == "nt" else [exe]

    def _script_cmd(self, info: dict[str, Any]) -> list[str]:
        pm = info["pm"]
        return [*self._pm_cmd(pm), info["script"]] if pm == "yarn" else [*self._pm_cmd(pm), "run", info["script"]]

    def _env(self) -> dict[str, str]:
        return {**os.environ, "BROWSER": "none", "FORCE_COLOR": "0", "NO_COLOR": "1", "NEXT_TELEMETRY_DISABLED": "1"}

    def _popen(self, cmd: list[str] | str, shell: bool = False) -> subprocess.Popen:
        cwd = self.root / (self.info or {}).get("dir", "")
        kw: dict[str, Any] = {"cwd": str(cwd), "env": self._env(), "stdout": subprocess.PIPE, "stderr": subprocess.STDOUT,
                              "stdin": subprocess.DEVNULL, "shell": shell}
        if os.name == "nt":
            kw["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        else:
            kw["start_new_session"] = True
        return subprocess.Popen(cmd, **kw)

    def _spawn(self, cmd: list[str] | str, shell: bool = False) -> None:
        shown = cmd if isinstance(cmd, str) else " ".join(Path(cmd[0]).name if i == 0 else c for i, c in enumerate(cmd))
        self._say(f"$ {shown}")
        try:
            self.proc = self._popen(cmd, shell=shell)
        except OSError as e:
            self.state, self.error = "failed", f"couldn't start: {e}"
            self._say(self.error)
            self._changed()
            return
        self.state, self.exit_code, self.error = "starting", None, None
        self.url = self.fixed_url
        threading.Thread(target=self._pump, args=(self.proc, True), daemon=True).start()
        if self.fixed_url:
            threading.Thread(target=self._wait_for_url, args=(self.proc,), daemon=True).start()
        self._changed()

    def _run(self, cmd: list[str], then_start: bool) -> None:
        """Install dependencies, then (optionally) start."""
        self._say("$ " + " ".join(Path(cmd[0]).name if i == 0 else c for i, c in enumerate(cmd)))
        try:
            proc = self._popen(cmd)
        except OSError as e:
            self.state, self.error = "failed", f"couldn't run the install: {e}"
            self._say(self.error)
            self._changed()
            return
        self.proc = proc
        self.state, self.error = "installing", None
        self._changed()

        def after() -> None:
            self._pump(proc, False)
            code = proc.wait()
            with self.lock:
                if self.proc is not proc:
                    return  # stopped meanwhile
                self.proc = None
                if code != 0:
                    self.state, self.exit_code = "failed", code
                    self.error = f"installing dependencies failed (exit code {code})"
                    self._changed()
                    return
                if self.info:
                    self.info["needs_install"] = False
                self.state = "idle"
            self._changed()
            if then_start:
                self.start()

        threading.Thread(target=after, daemon=True).start()

    def _pump(self, proc: subprocess.Popen, watch_url: bool) -> None:
        assert proc.stdout is not None
        last = 0.0
        for raw in iter(proc.stdout.readline, b""):
            line = ANSI.sub("", raw.decode("utf8", "replace")).rstrip()
            if not line:
                continue
            self._say(line)
            if watch_url and self.url is None:
                m = URL.search(line)  # only ever matches this machine's addresses
                if m and "network" not in line.lower():
                    url = m.group(0).replace("0.0.0.0", "localhost").replace("[::1]", "localhost").replace("[::]", "localhost")
                    with self.lock:
                        if self.proc is proc:
                            self.url, self.state = url, "running"
                    self._changed()
                    continue
            if time.monotonic() - last > 0.5:  # stream the log, but not every line
                last = time.monotonic()
                self._changed()
        if watch_url:
            code = proc.wait()
            with self.lock:
                if self.proc is not proc:
                    return
                self.proc = None
                self.state, self.exit_code = ("exited" if code == 0 else "failed"), code
                self.error = None if code == 0 else f"the app stopped with exit code {code}"
                self.url = self.fixed_url if self.info and self.info["kind"] == "url" else None
            self._changed()

    def _wait_for_url(self, proc: subprocess.Popen) -> None:
        """With --ui-url, the app is up once that URL answers."""
        import urllib.request

        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for _ in range(240):
            if self.proc is not proc or proc.poll() is not None:
                return
            try:
                opener.open(self.fixed_url, timeout=1).close()
                with self.lock:
                    if self.proc is proc:
                        self.state = "running"
                self._changed()
                return
            except Exception as e:  # an HTTP error still means it's up
                if getattr(e, "code", None):
                    with self.lock:
                        if self.proc is proc:
                            self.state = "running"
                    self._changed()
                    return
            time.sleep(0.5)

    def _start_static(self) -> None:
        folder = self.root / self.info["dir"]
        handler = partial(_Quiet, directory=str(folder))
        self.static = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.static.daemon_threads = True
        threading.Thread(target=self.static.serve_forever, daemon=True).start()
        self.url = f"http://localhost:{self.static.server_address[1]}/"
        self.state = "running"
        self._say(f"Serving {self.info['dir'] or '.'}/ as a static site at {self.url}")
        self._changed()


def _kill_tree(proc: subprocess.Popen) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, timeout=15)
        else:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
