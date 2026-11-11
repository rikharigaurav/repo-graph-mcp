import http.client
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from repo_graph_mcp import live as L
from repo_graph_mcp.apprunner import AppRunner, detect
from repo_graph_mcp.builder import build_graph
from repo_graph_mcp.review import Review, message_for_claude, reviewable

ROOT = Path(__file__).resolve().parents[1]
NOTIFY = ROOT / "skills/graph-repo/scripts/live_notify.py"


def write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    st = p.stat()
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000))


@pytest.fixture()
def web(tmp_path: Path) -> Path:
    root = tmp_path / "web"
    write(root, "src/App.jsx", "export default function App() {\n  return <h1>Hello</h1>\n}\n")
    write(root, "src/index.css", "h1 { color: black; }\n")
    write(root, "package.json", json.dumps({"scripts": {"dev": "vite"}, "devDependencies": {"vite": "5"}}))
    write(root, ".env", "SECRET=1\n")
    write(root, "package-lock.json", "{}")
    return root


def test_reviewable_skips_secrets_locks_and_junk():
    assert reviewable("src/App.jsx") and reviewable("src/index.css") and reviewable("package.json")
    assert not reviewable(".env") and not reviewable("package-lock.json") and not reviewable("logo.png")
    assert not reviewable("vite.config.js.timestamp-1712.mjs") and not reviewable("app.log")


def test_review_accept_undo_and_bring_back(web: Path):
    sc = L.Scanner(web)
    r = Review(web, sc.snapshot())
    assert r.pending(sc.snapshot()) == []
    write(web, "src/App.jsx", "export default function App() {\n  return <h1>Shop</h1>\n}\n")
    write(web, "src/index.css", "h1 { color: red; }\n")
    write(web, "src/Cart.jsx", "export const Cart = () => null\n")
    (web / "package.json").unlink()
    got = {p["path"]: (p["status"], p["adds"], p["dels"]) for p in r.pending(sc.snapshot())}
    assert got == {"src/App.jsx": ("modified", 1, 1), "src/index.css": ("modified", 1, 1),
                   "src/Cart.jsx": ("added", 1, 0), "package.json": ("deleted", 0, 1)}
    d = r.diff("src/App.jsx")["lines"]
    assert "-  return <h1>Hello</h1>" in d and "+  return <h1>Shop</h1>" in d

    r.accept(["src/index.css"], sc.snapshot())
    r.undo(["src/App.jsx", "src/Cart.jsx", "package.json"],
           {"src/App.jsx": {"source": "claude", "tools": ["Edit"]}})
    assert "Hello" in (web / "src/App.jsx").read_text()
    assert not (web / "src/Cart.jsx").exists() and (web / "package.json").exists()
    assert r.pending(sc.snapshot()) == []  # the accepted CSS is the new baseline
    assert r.ours("src/App.jsx") == "undo"

    msg = message_for_claude(r.take_unreported())
    assert "src/App.jsx: your change (editing it) was undone" in msg
    assert "src/Cart.jsx: a change (creating it) was undone" in msg
    assert r.take_unreported() == []  # told once

    assert r.bring_back("src/App.jsx")
    assert "Shop" in (web / "src/App.jsx").read_text()
    assert [p["path"] for p in r.pending(sc.snapshot())] == ["src/App.jsx"]


def test_review_refuses_paths_outside_the_folder(web: Path, tmp_path: Path):
    r = Review(web, L.Scanner(web).snapshot())
    write(tmp_path, "outside.js", "x")
    with pytest.raises(ValueError):
        r.undo(["../outside.js"])


def test_detect_frontends(tmp_path: Path):
    a = tmp_path / "a"
    write(a, "package.json", json.dumps({"scripts": {"build": "tsc"}}))  # a library: no dev server
    write(a, "frontend/package.json", json.dumps({"scripts": {"dev": "next dev", "start": "next start"},
                                                  "dependencies": {"next": "14", "react": "18"}}))
    write(a, "frontend/pnpm-lock.yaml", "")
    info = detect(a)
    assert info["framework"] == "Next.js" and info["dir"] == "frontend" and info["cmd"] == "pnpm run dev"
    assert info["needs_install"] is True
    b = tmp_path / "b"
    write(b, "site/index.html", "<h1>hi</h1>")
    assert detect(b)["kind"] == "static" and detect(b)["dir"] == "site"
    c = tmp_path / "c"
    write(c, "main.py", "print(1)")
    assert detect(c) is None


def test_runner_starts_a_custom_command_and_stops_it(tmp_path: Path):
    seen = []
    cmd = f'"{sys.executable}" -u -m http.server 0 --bind 127.0.0.1'
    run = AppRunner(tmp_path, cmd=cmd, on_change=lambda s: seen.append(s["state"]))
    run.start()
    deadline = time.time() + 20
    while run.status()["state"] != "running" and time.time() < deadline:
        time.sleep(0.1)
    st = run.status()
    assert st["state"] == "running" and st["url"].startswith("http://127.0.0.1:"), st
    port = int(st["url"].rsplit(":", 1)[1].strip("/"))
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("GET", "/")
    assert c.getresponse().status == 200
    run.stop()
    time.sleep(0.5)
    with pytest.raises(OSError):
        http.client.HTTPConnection("127.0.0.1", port, timeout=2).request("GET", "/")
    assert "starting" in seen and "running" in seen


def test_static_site_is_served_on_its_own_port(tmp_path: Path):
    write(tmp_path, "index.html", "<h1>static</h1>")
    run = AppRunner(tmp_path)
    run.start()
    st = run.status()
    assert st["state"] == "running" and st["app"]["kind"] == "static"
    port = int(st["url"].rsplit(":", 1)[1].strip("/"))
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("GET", "/")
    assert b"static" in c.getresponse().read()
    run.stop()


def _post(port: int, path: str, body: dict, headers: dict) -> tuple[int, dict]:
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request("POST", path, body=json.dumps(body), headers={"Host": f"127.0.0.1:{port}", **headers})
    r = c.getresponse()
    data = r.read()
    return r.status, (json.loads(data) if data and r.getheader("Content-Type", "").startswith("application/json") else {})


def _get(port: int, path: str) -> dict:
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request("GET", path, headers={"Host": f"127.0.0.1:{port}"})
    return json.loads(c.getresponse().read())


def test_server_review_endpoints_and_claude_feedback(web: Path, tmp_path: Path):
    home = tmp_path / "home"
    s = L.LiveSession(web, lambda: (build_graph(web, {"name": "web"}), None), interval=0.2)
    srv = L.LiveServer(s, lambda p: "<html></html>", graph_id="live:web", port=0, home=home,
                       app=AppRunner(web))
    srv.start()
    try:
        page = {"X-Repo-Graph-Page": s.page_token}
        assert json.loads(s.payload_bytes("x"))["live"]["token"] == s.page_token
        assert _get(srv.port, "/api/app")["app"]["framework"] == "Vite"

        write(web, "src/App.jsx", "export default function App() {\n  return <h1>Shop</h1>\n}\n")
        hook = {"cwd": str(web), "tool_name": "Edit", "tool_input": {"file_path": "src/App.jsx"}}
        subprocess.run([sys.executable, str(NOTIFY)], input=json.dumps(hook), text=True, timeout=20,
                       env={**os.environ, "REPO_GRAPH_HOME": str(home)})
        write(web, "src/index.css", "h1 { color: red; }\n")
        deadline = time.time() + 10
        while len(_get(srv.port, "/api/review")["pending"]) < 2 and time.time() < deadline:
            time.sleep(0.2)
        pending = {p["path"]: p for p in _get(srv.port, "/api/review")["pending"]}
        assert pending["src/App.jsx"]["source"] == "claude" and pending["src/App.jsx"]["tools"] == ["Edit"]
        assert pending["src/index.css"]["source"] == "disk"  # credit is per file, not per change
        assert "+  return <h1>Shop</h1>" in _get(srv.port, "/api/review/diff?path=src/App.jsx")["lines"]

        assert _post(srv.port, "/api/review/undo", {"paths": ["src/App.jsx"]}, {})[0] == 403  # no page token
        assert _post(srv.port, "/api/review/undo", {"paths": ["src/App.jsx"]},
                     {"X-Repo-Graph-Token": srv.token})[0] == 403  # the hook's token isn't the page's
        status, out = _post(srv.port, "/api/review/undo", {"paths": ["src/App.jsx"]}, page)
        assert status == 200 and out["done"] == ["src/App.jsx"] and "Hello" in (web / "src/App.jsx").read_text()
        status, out = _post(srv.port, "/api/review/accept", {"all": True}, page)
        assert out["done"] == ["src/index.css"]

        # before the next prompt, Claude Code's hook asks what was undone
        p = subprocess.run([sys.executable, str(NOTIFY), "--context"], input=json.dumps({"cwd": str(web)}), text=True,
                           capture_output=True, timeout=20, env={**os.environ, "REPO_GRAPH_HOME": str(home)})
        assert p.returncode == 0 and "src/App.jsx: your change (editing it) was undone" in p.stdout
        p = subprocess.run([sys.executable, str(NOTIFY), "--context"], input=json.dumps({"cwd": str(web)}), text=True,
                           capture_output=True, timeout=20, env={**os.environ, "REPO_GRAPH_HOME": str(home)})
        assert p.stdout == ""

        deadline = time.time() + 10
        while time.time() < deadline and not any(e["source"] == "review" for e in s.hello()["events"]):
            time.sleep(0.2)
        assert any(e["source"] == "review" and e["title"] == "Undid changes to App.jsx" for e in s.hello()["events"])
    finally:
        srv.close()


def test_context_hook_is_quiet_without_a_live_view(tmp_path: Path):
    p = subprocess.run([sys.executable, str(NOTIFY), "--context"], input=json.dumps({"cwd": str(tmp_path)}),
                       text=True, capture_output=True, timeout=20, env={**os.environ, "REPO_GRAPH_HOME": str(tmp_path / "x")})
    assert p.returncode == 0 and p.stdout == "" and p.stderr == ""
