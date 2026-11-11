import http.client
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from repo_graph_mcp import live as L
from repo_graph_mcp.builder import build_graph
from repo_graph_mcp.parsecache import ParseCache

ROOT = Path(__file__).resolve().parents[1]
NOTIFY = ROOT / "skills/graph-repo/scripts/live_notify.py"
GIT_ENV = {"GIT_AUTHOR_NAME": "Dev", "GIT_AUTHOR_EMAIL": "dev@example.com", "GIT_COMMITTER_NAME": "Dev",
           "GIT_COMMITTER_EMAIL": "dev@example.com"}


def write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    # some filesystems keep coarse mtimes: make sure each write looks new
    st = p.stat()
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000))


@pytest.fixture()
def app(tmp_path: Path) -> Path:
    root = tmp_path / "app"
    write(root, "app/api.py", "def ping():\n    return 'pong'\n")
    write(root, "app/db.py", "def connect():\n    return None\n")
    write(root, "README.md", "# app\n")
    return root


def session(root: Path, tmp_path: Path, rebuild=None) -> L.LiveSession:
    pc = ParseCache(tmp_path / "parses.json.gz")
    rebuild = rebuild or (lambda: (build_graph(root, {"name": "app"}, parse_cache=pc), None))
    return L.LiveSession(root, rebuild, interval=0.2, save=pc.save)


def test_scanner_sees_added_changed_and_deleted_files(app: Path):
    sc = L.Scanner(app)
    before = sc.snapshot()
    assert set(before) == {"app/api.py", "app/db.py", "README.md"}
    write(app, "app/api.py", "def ping():\n    return 1\n")
    write(app, "app/jobs.py", "def nightly():\n    pass\n")
    write(app, "image.png", "not graphed")
    (app / "app/db.py").unlink()
    assert L.Scanner.changes(before, sc.snapshot()) == [
        {"path": "app/api.py", "status": "modified"},
        {"path": "app/db.py", "status": "deleted"},
        {"path": "app/jobs.py", "status": "added"},
    ]


def test_tick_turns_an_edit_into_a_graph_change(app: Path, tmp_path: Path):
    s = session(app, tmp_path)
    assert s.tick() is None  # nothing changed yet
    write(app, "app/api.py", "import os\n\ndef ping():\n    return os.environ['PING_URL']\n\ndef pong():\n    return ping()\n")
    ev = s.tick()
    assert ev and ev["source"] == "disk" and ev["title"] == "Edited api.py"
    assert ev["files"] == [{"path": "app/api.py", "status": "modified"}]
    added = {r["id"] for r in ev["diff"]["added"]}
    assert "FUNCTION:app/api.py:pong" in added and "ENV:PING_URL" in added
    assert [r["id"] for r in ev["diff"]["changed"] if r["l"] == "Function"] == ["FUNCTION:app/api.py:ping"]
    assert "new env var PING_URL" in ev["summary"]
    since = s.since()
    assert since["changes"] == 1 and since["files"] == ev["files"]
    payload = json.loads(s.payload_bytes("live:app"))
    assert payload["live"]["seq"] == 1 and any(n["id"] == "FUNCTION:app/api.py:pong" for n in payload["nodes"])


def test_hook_notice_labels_the_change_as_claudes(app: Path, tmp_path: Path):
    s = session(app, tmp_path)
    write(app, "app/db.py", "def connect():\n    return 1\n")
    s.notify(str(app / "app/db.py"), "Edit")  # the hook usually arrives first
    ev = s.tick()
    assert ev["source"] == "claude" and ev["tools"] == ["Edit"] and s.hook_seen
    # …but when the poll wins the race, the late notice relabels the change it caught
    write(app, "app/api.py", "def ping():\n    return 2\n")
    ev2 = s.tick()
    assert ev2["source"] == "disk"
    s.notify(str(app / "app" / ".." / "app" / "api.py"), "Write")
    assert ev2["source"] == "claude" and ev2["tools"] == ["Write"]
    assert s.since()["by_claude"] == 2


@pytest.mark.skipif(not shutil.which("git"), reason="git not installed")
def test_a_commit_shows_as_its_own_change(app: Path, tmp_path: Path):
    def git(*a):
        subprocess.run(["git", "-C", str(app), *a], check=True, capture_output=True, env={**os.environ, **GIT_ENV})

    git("init", "-q", "-b", "main")
    git("add", "-A")
    git("commit", "-qm", "First")
    from repo_graph_mcp import history as H
    from repo_graph_mcp.store import GraphStore

    snaps = H.Snapshots(GraphStore(max_items=4, disk_dir=str(tmp_path / "snaps")))

    def rebuild():
        return H.build_history(app, {"name": "app"}, lambda sha: f"app@{sha[:12]}", build_graph, snaps, 3)

    s = session(app, tmp_path, rebuild)
    write(app, "app/jobs.py", "def nightly():\n    pass\n")
    ev = s.tick()
    assert ev["source"] == "disk" and s.history["dirty"]
    git("add", "-A")
    git("commit", "-qm", "Add nightly job")
    ev = s.tick()
    assert ev["source"] == "git" and ev["title"].endswith("· Add nightly job")
    assert "FUNCTION:app/jobs.py:nightly" in {r["id"] for r in ev["diff"]["added"]}
    assert not s.history["dirty"] and s.history["entries"][0]["title"] == "Add nightly job"


def _get(port: int, path: str, host: str | None = None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("GET", path, headers={"Host": host or f"127.0.0.1:{port}"})
    r = c.getresponse()
    return r.status, r.read()


def _post(port: int, path: str, body: dict, token: str = ""):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("POST", path, body=json.dumps(body), headers={"Host": f"127.0.0.1:{port}", "X-Repo-Graph-Token": token,
                                                             "Content-Type": "application/json"})
    r = c.getresponse()
    return r.status


def test_server_page_stream_and_guards(app: Path, tmp_path: Path):
    home = tmp_path / "home"
    s = session(app, tmp_path)
    srv = L.LiveServer(s, lambda payload: "<html>" + payload.decode() + "</html>", graph_id="live:app", port=0, home=home)
    srv.start()
    try:
        status, body = _get(srv.port, "/")
        assert status == 200 and b'"live":{' in body
        assert _get(srv.port, "/api/state", host="attacker.example:80")[0] == 421  # DNS rebinding
        assert _post(srv.port, "/api/notify", {"file": str(app / "app/api.py"), "tool": "Edit"}) == 403  # no token
        lock = L.find_running(home, app)
        assert lock and lock["port"] == srv.port and lock["token"] == srv.token

        # the stream: a hello first, then a change after an edit
        c = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=10)
        c.request("GET", "/api/events", headers={"Host": f"127.0.0.1:{srv.port}"})
        r = c.getresponse()
        assert r.getheader("Content-Type").startswith("text/event-stream")

        def next_event():
            name = data = None
            while True:
                line = r.fp.readline().decode().rstrip("\n")
                if line.startswith("event: "):
                    name = line[7:]
                elif line.startswith("data: "):
                    data = json.loads(line[6:])
                elif line == "" and name:
                    return name, data

        assert next_event()[0] == "hello"
        write(app, "app/db.py", "def connect():\n    return 'db'\n")
        # the hook script finds the server through its lock file and labels the edit
        hook = {"cwd": str(app), "tool_name": "Edit", "tool_input": {"file_path": "app/db.py"}}
        p = subprocess.run([sys.executable, str(NOTIFY)], input=json.dumps(hook), capture_output=True, text=True,
                           env={**os.environ, "REPO_GRAPH_HOME": str(home)}, timeout=20)
        assert p.returncode == 0 and p.stdout == ""
        seen = {}
        deadline = time.time() + 10
        while "change" not in seen and time.time() < deadline:
            name, data = next_event()
            seen[name] = data
        ev = seen["change"]["event"]
        assert ev["source"] == "claude" and ev["tools"] == ["Edit"] and ev["files"][0]["path"] == "app/db.py"
        c.close()

        assert _post(srv.port, "/api/stop", {}, srv.token) == 204
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                _get(srv.port, "/api/ping")
            except OSError:
                break
            time.sleep(0.1)
    finally:
        srv.close()
    assert not L.lock_path(home, app).exists()


def test_notify_hook_is_quiet_without_a_live_view(tmp_path: Path):
    for stdin in ("not json", json.dumps({"tool_name": "Edit", "tool_input": {"file_path": "/x/y.py"}}), ""):
        p = subprocess.run([sys.executable, str(NOTIFY)], input=stdin, capture_output=True, text=True,
                           env={**os.environ, "REPO_GRAPH_HOME": str(tmp_path / "none")}, timeout=20)
        assert p.returncode == 0 and p.stdout == "" and p.stderr == ""


def test_hooks_json_points_at_the_notify_script():
    hooks = json.loads((ROOT / "hooks/hooks.json").read_text())
    entry = hooks["hooks"]["PostToolUse"][0]
    assert set(entry["matcher"].split("|")) == {"Edit", "Write", "MultiEdit", "NotebookEdit"}
    h = entry["hooks"][0]
    assert h["async"] is True and "${CLAUDE_PLUGIN_ROOT}/skills/graph-repo/scripts/live_notify.py" in h["command"]


def test_cli_stop_without_a_server(app: Path, tmp_path: Path):
    p = subprocess.run([sys.executable, str(ROOT / "skills/graph-repo/scripts/graph_repo.py"), "live", str(app), "--stop"],
                       capture_output=True, text=True, env={**os.environ, "REPO_GRAPH_HOME": str(tmp_path / "home")},
                       timeout=60)
    assert p.returncode == 0 and "no live view is running" in p.stdout
