import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from repo_graph_mcp import graph as G
from repo_graph_mcp import history as H
from repo_graph_mcp.builder import build_graph
from repo_graph_mcp.parsecache import ParseCache
from repo_graph_mcp.store import GraphStore
from repo_graph_mcp.summary import summarize

pytestmark = pytest.mark.skipif(not shutil.which("git"), reason="git not installed")
ROOT = Path(__file__).resolve().parents[1]


def sh(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          env={"GIT_AUTHOR_NAME": "Dev", "GIT_AUTHOR_EMAIL": "dev@example.com",
                               "GIT_COMMITTER_NAME": "Dev", "GIT_COMMITTER_EMAIL": "dev@example.com",
                               "HOME": str(cwd), "PATH": __import__("os").environ["PATH"]}).stdout


def write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


@pytest.fixture()
def repo(tmp_path: Path):
    """origin (bare) <- clone with: c1 pushed, c2 + c3 local only, plus uncommitted work."""
    origin = tmp_path / "origin.git"
    work = tmp_path / "app"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    work.mkdir()
    sh(work, "init", "-q", "-b", "main")
    sh(work, "remote", "add", "origin", str(origin))
    write(work, "app/api.py", "def ping():\n    return 'pong'\n\n\ndef old_helper():\n    return 1\n")
    write(work, "app/db.py", "def connect():\n    return None\n")
    sh(work, "add", "-A")
    sh(work, "commit", "-qm", "First version")
    sh(work, "push", "-q", "-u", "origin", "main")
    # c2: change ping, remove old_helper, add a service
    write(work, "app/api.py", "from openai import OpenAI\n\nclient = OpenAI()\n\n\ndef ping():\n    return client.models.list()\n")
    sh(work, "commit", "-qam", "Ping the model")
    # c3: new file with a new function
    write(work, "app/jobs.py", "from app.api import ping\n\n\ndef nightly():\n    return ping()\n")
    sh(work, "add", "-A")
    sh(work, "commit", "-qm", "Add nightly job")
    # uncommitted: edit connect, add an env read
    write(work, "app/db.py", "import os\n\n\ndef connect():\n    return os.environ['DATABASE_URL']\n")
    return work


def run(repo: Path, tmp_path: Path, builds: list | None = None, commits: int = 5):
    store = GraphStore(max_items=4, disk_dir=str(tmp_path / "cache"))
    pc = ParseCache(tmp_path / "cache" / "parses.json.gz")

    def build(folder, info):
        if builds is not None:
            builds.append(info.get("sha"))
        return build_graph(folder, info, parse_cache=pc)

    info = {"key": "app@now", "display": "app", "name": "app"}
    g, hist = H.build_history(repo, info, lambda sha: f"app@{sha[:12]}", build, H.Snapshots(store), commits,
                              commit_url=lambda sha: f"https://github.com/acme/app/commit/{sha}",
                              cache_dir=tmp_path / "cache" / "history")
    pc.save()
    return g, hist, pc


def test_state_branch_upstream_and_working(repo: Path):
    st = H.repo_state(repo, 5)
    assert st["branch"] == "main" and st["upstream"] == "origin/main"
    assert st["ahead"] == 2 and st["behind"] == 0
    assert st["working"] == [{"path": "app/db.py", "status": "modified"}]
    assert [c["subject"] for c in st["commits"]] == ["Add nightly job", "Ping the model", "First version"]
    assert [c["pushed"] for c in st["commits"]] == [False, False, True]


def test_history_entries_are_graph_changes(repo: Path, tmp_path: Path):
    g, hist, _ = run(repo, tmp_path)
    assert g.repo["ref"] == "main*"
    assert hist["ahead"] == 2 and hist["dirty"]
    working, c3, c2, c1 = hist["entries"]
    assert working["kind"] == "working"
    assert [r["id"] for r in working["diff"]["changed"] if r["l"] == G.FUNCTION] == ["FUNCTION:app/db.py:connect"]
    assert any(r["l"] == G.ENV_VAR and r["n"] == "DATABASE_URL" for r in working["diff"]["added"])

    assert c3["title"] == "Add nightly job" and c3["pushed"] is False
    assert {r["id"] for r in c3["diff"]["added"]} >= {"FUNCTION:app/jobs.py:nightly", "SOURCECODEFILE:app/jobs.py:py"}
    assert c3["url"] == f"https://github.com/acme/app/commit/{c3['sha']}"

    added = {r["id"] for r in c2["diff"]["added"]}
    assert "SERVICE:OpenAI" in added
    assert [r["id"] for r in c2["diff"]["removed"]] == ["FUNCTION:app/api.py:old_helper"]
    assert [r["id"] for r in c2["diff"]["changed"] if r["l"] == G.FUNCTION] == ["FUNCTION:app/api.py:ping"]
    assert "new service OpenAI" in c2["summary"]

    assert c1["pushed"] is True  # first commit: everything is new
    assert {"FUNCTION:app/api.py:ping", "FUNCTION:app/db.py:connect"} <= {r["id"] for r in c1["diff"]["added"]}

    text = summarize(g, None, 0.1, False, False, history=hist)
    assert "Version control: main @" in text and "2 commits not pushed to origin/main" in text
    assert "Uncommitted (1 files: app/db.py)" in text and "[not pushed]" in text


def test_second_run_reuses_snapshots_and_parses(repo: Path, tmp_path: Path):
    first: list = []
    run(repo, tmp_path, first)
    assert len(first) == 4  # the 3 commits, then the working tree on top of HEAD
    second: list = []
    _, hist, pc = run(repo, tmp_path, second)
    assert second == [hist["head"]]  # only the uncommitted working tree is rebuilt
    assert pc.misses == 0 and pc.hits > 0  # and none of its files are parsed again
    sh(repo, "commit", "-qam", "Read the database URL")
    third: list = []
    g, hist, _ = run(repo, tmp_path, third)
    assert third == [hist["head"]]  # the new commit; everything older comes from snapshots
    assert not hist["dirty"] and hist["entries"][0]["title"] == "Read the database URL"
    fourth: list = []
    run(repo, tmp_path, fourth)
    assert fourth == []  # clean and already built: nothing to build at all


def test_subfolder_history(repo: Path, tmp_path: Path):
    g, hist, _ = run(repo / "app", tmp_path)
    assert {n.path for n in g.nodes.values() if n.label == G.SOURCE_FILE} == {"api.py", "db.py", "jobs.py"}
    assert hist["entries"][0]["files"] == [{"path": "db.py", "status": "modified"}]


def test_not_a_git_repo_builds_plainly(tmp_path: Path):
    write(tmp_path / "plain", "x.py", "def f():\n    return 1\n")
    g, hist = H.build_history(tmp_path / "plain", {"name": "plain"}, lambda s: s, build_graph,
                              H.Snapshots(GraphStore(max_items=2)), 3)
    assert hist is None and "FUNCTION:x.py:f" in g.nodes


def test_cli_writes_history(repo: Path, tmp_path: Path):
    out = tmp_path / "out"
    env = {**__import__("os").environ, "REPO_GRAPH_HOME": str(tmp_path / "home")}
    p = subprocess.run([sys.executable, str(ROOT / "skills/graph-repo/scripts/graph_repo.py"), "build", str(repo),
                        "--out", str(out), "--history", "3"], capture_output=True, text=True, env=env)
    assert p.returncode == 0, p.stderr
    hist = json.loads((out / "history.json").read_text())
    assert [e["kind"] for e in hist["entries"]] == ["working", "commit", "commit", "commit"]
    assert '"history":' in (out / "graph.html").read_text()
    assert "Version control: main @" in p.stdout
    p2 = subprocess.run([sys.executable, str(ROOT / "skills/graph-repo/scripts/graph_repo.py"), "build", str(repo),
                         "--out", str(out), "--history", "0"], capture_output=True, text=True, env=env)
    assert p2.returncode == 0 and not (out / "history.json").exists()


def test_diff_graphs_flags_signature_changes():
    a, b = G.CodeGraph({}), G.CodeGraph({})
    a.add_node(G.Node("FUNCTION:x.py:f", G.FUNCTION, "f", "x.py", {"h": "1", "signature": "f(a)"}))
    b.add_node(G.Node("FUNCTION:x.py:f", G.FUNCTION, "f", "x.py", {"h": "2", "signature": "f(a, b)"}))
    d = H.diff_graphs(a, b)
    assert d["changed"] == [{"id": "FUNCTION:x.py:f", "l": G.FUNCTION, "n": "f", "p": "x.py", "sig": True}]
    assert d["counts"] == {G.FUNCTION: [0, 1, 0]}
