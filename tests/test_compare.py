import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from repo_graph_mcp import compare as C
from repo_graph_mcp import fetch as F
from repo_graph_mcp import graph as G
from repo_graph_mcp import history as H
from repo_graph_mcp.builder import build_graph
from repo_graph_mcp.store import GraphStore

pytestmark = pytest.mark.skipif(not shutil.which("git"), reason="git not installed")
ROOT = Path(__file__).resolve().parents[1]
ENV = {"GIT_AUTHOR_NAME": "Dev", "GIT_AUTHOR_EMAIL": "dev@example.com", "GIT_COMMITTER_NAME": "Dev",
       "GIT_COMMITTER_EMAIL": "dev@example.com", "PATH": os.environ["PATH"]}


def sh(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          env={**ENV, "HOME": str(cwd)}).stdout.strip()


def write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


MOVER = "def mover():\n    x = 1\n    y = 2\n    z = x + y\n    return z * 2\n"


@pytest.fixture()
def repo(tmp_path: Path):
    """main: c1 (base). feature: c2, which breaks callers in a file it doesn't touch."""
    work = tmp_path / "app"
    work.mkdir()
    sh(work, "init", "-q", "-b", "main")
    write(work, "app/util.py", "def helper(a):\n    return a + 1\n\n\ndef old_fn():\n    return 2\n\n\n" + MOVER)
    write(work, "app/main.py", "from app.util import helper, old_fn, mover\n\n\ndef run():\n"
                               "    return helper(1) + old_fn() + mover()\n")
    write(work, "tests/test_util.py", "from app.util import helper\n\n\ndef test_helper():\n    assert helper(1) == 2\n")
    write(work, "requirements.txt", "requests==2.31\n")
    sh(work, "add", "-A")
    sh(work, "commit", "-q", "-m", "c1")
    sh(work, "checkout", "-q", "-b", "feature")
    write(work, "app/util.py", "def helper(a, b):\n    return a + b\n")  # new signature, old_fn gone, mover moved
    write(work, "app/other.py", "import os\nimport openai\n\n\n" + MOVER + "\n\ndef ask():\n"
                                "    key = os.getenv('OPENAI_API_KEY')\n    return openai.OpenAI(api_key=key)\n")
    write(work, "requirements.txt", "requests==2.32\nopenai>=1.0\n")
    sh(work, "add", "-A")
    sh(work, "commit", "-q", "-m", "c2: refactor util, ask the model")
    return work


def run(work: Path, tmp_path: Path, **kw):
    spec = F.parse_repo_spec(str(work))
    snaps = H.Snapshots(GraphStore(max_items=4, disk_dir=str(tmp_path / "cache")))
    info = {"display": spec.display, "name": "app", "owner": "", "ref": "", "source": "local", "built_at": 0}
    return C.run(spec, kw.get("base"), kw.get("head"), kw.get("pr"), info=info,
                 build=lambda folder, inf: build_graph(folder, inf), snapshots=snaps,
                 key_for=lambda sha: f"local:app@{sha[:12]}")


def names(lst):
    return sorted(r.get("q") or r["n"] for r in lst)


def test_branch_defaults_to_where_it_branched(repo: Path, tmp_path: Path):
    g, rep, hist = run(repo, tmp_path, head="feature")
    assert rep["base"] == sh(repo, "rev-parse", "main")
    assert rep["head"] == sh(repo, "rev-parse", "feature")
    assert rep["base_label"] == "where it branched from main"
    assert [c["subject"] for c in rep["commits"]] == ["c2: refactor util, ask the model"]
    assert {f["path"]: f["status"] for f in rep["files"]} == {
        "app/other.py": "added", "app/util.py": "modified", "requirements.txt": "modified"}


def test_finds_what_the_change_broke(repo: Path, tmp_path: Path):
    g, rep, hist = run(repo, tmp_path, base="main", head="feature")
    # mover() kept its lines but now lives in other.py; old_fn() is simply gone
    assert [(m["from"]["p"], m["to"]["p"]) for m in rep["moved"]] == [("app/util.py", "app/other.py")]
    assert names(rep["removed"]) == ["old_fn"]
    assert "ask" in names(rep["added"]) and "mover" not in names(rep["added"])
    # main.py wasn't touched, but still calls old_fn() and the old helper(a)
    still = {r["n"]: r for r in rep["still_called"]}
    assert set(still) >= {"old_fn"}
    assert [c["n"] for c in still["old_fn"]["callers"]] == ["run"]
    sig = {r["n"]: r for r in rep["signature_changes"]}
    assert sig["helper"]["before"].startswith("helper(a)") and sig["helper"]["after"].startswith("helper(a, b)")
    assert [c["n"] for c in sig["helper"]["callers"]] == ["run"]
    assert sig["helper"]["tests"] == ["tests/test_util.py"]
    # tests: helper is tested; ask() isn't; test_util.py is the one to run
    assert "ask" in names(rep["coverage"]["none"])
    assert "helper" in names(rep["coverage"]["direct"])
    assert [t["path"] for t in rep["tests_to_run"]] == ["tests/test_util.py"]
    # the outside world: a new service, a new env var, a new package and a bumped one
    assert [r["n"] for r in rep["surface"][G.SERVICE]["added"]] == ["OpenAI"]
    assert [r["n"] for r in rep["surface"][G.ENV_VAR]["added"]] == ["OPENAI_API_KEY"]
    assert rep["dependencies"] == [{"file": "requirements.txt", "added": [{"name": "openai", "spec": ">=1.0"}],
                                    "removed": [], "changed": [{"name": "requests", "from": "==2.31", "to": "==2.32"}]}]
    first = "\n".join(rep["check_first"])
    assert "`old_fn()` was removed" in first and "`helper()` changed its signature" in first
    assert "`openai`" in first and "no test reaches" in first


def test_markdown_and_view(repo: Path, tmp_path: Path):
    g, rep, hist = run(repo, tmp_path, base="main", head="feature")
    md = C.to_markdown(rep, "app")
    assert md.startswith("# Changes in app: main → feature")
    for heading in ("## Check first", "## Moved", "## Removed", "## Signature changes", "## Tests to run",
                    "## Endpoints, services and config", "## Packages", "## Commits"):
        assert heading in md
    (entry,) = hist["entries"]
    assert entry["kind"] == "range" and entry["commits"] == 1 and entry["diff"]["added"]
    assert hist["range"]["label"] == rep["short"]
    assert any(r["id"] in g.nodes for r in entry["diff"]["added"])  # marks land on the head's graph


def test_working_tree_against_head(repo: Path, tmp_path: Path):
    write(repo, "app/util.py", "def helper(a, b, c=0):\n    return a + b + c\n")
    write(repo, "app/new.py", "def fresh():\n    return 1\n")
    g, rep, hist = run(repo, tmp_path)
    assert rep["head"] is None and rep["head_label"] == "working tree"
    assert rep["base"] == sh(repo, "rev-parse", "HEAD") and rep["base_label"] == "HEAD"
    assert {f["path"]: f["status"] for f in rep["files"]} == {"app/new.py": "added", "app/util.py": "modified"}
    assert names(rep["added"]) == ["fresh"]
    assert hist["dirty"] is True
    assert not rep["commits"]


def test_second_run_reuses_snapshots(repo: Path, tmp_path: Path):
    run(repo, tmp_path, base="main", head="feature")
    calls = []
    spec = F.parse_repo_spec(str(repo))
    snaps = H.Snapshots(GraphStore(max_items=4, disk_dir=str(tmp_path / "cache")))
    C.run(spec, "main", "feature", None, info={"display": spec.display, "name": "app", "source": "local"},
          build=lambda folder, inf: calls.append(inf["sha"]) or build_graph(folder, inf), snapshots=snaps,
          key_for=lambda sha: f"local:app@{sha[:12]}")
    assert calls == []


def test_bad_refs_and_pr_on_a_folder(repo: Path, tmp_path: Path):
    with pytest.raises(C.RangeError, match="no commit, branch or tag called 'nope'"):
        run(repo, tmp_path, base="nope")
    with pytest.raises(C.RangeError, match="pull request needs the repository's URL"):
        run(repo, tmp_path, pr=3)


def test_package_manifests():
    pkg = json.dumps({"dependencies": {"react": "^18"}, "devDependencies": {"vite": "^5"}})
    assert C._packages("web/package.json", pkg) == {"react": "^18", "vite": "^5 (dev)"}
    req = "# deps\nFlask_Login[extra]==0.6 ; python_version>'3'\n-r base.txt\nrequests\n"
    assert C._packages("requirements-dev.txt", req) == {"flask-login": "==0.6", "requests": ""}
    if sys.version_info >= (3, 11):
        toml = '[project]\ndependencies = ["httpx>=0.27", "mcp"]\n[project.optional-dependencies]\ndev = ["pytest"]\n'
        assert C._packages("pyproject.toml", toml) == {"httpx": ">=0.27", "mcp": "", "pytest": ""}


def test_cli_diff_writes_the_review(repo: Path, tmp_path: Path):
    out = tmp_path / "out"
    script = ROOT / "skills" / "graph-repo" / "scripts" / "graph_repo.py"
    env = {**os.environ, "REPO_GRAPH_HOME": str(tmp_path / "home")}
    p = subprocess.run([sys.executable, str(script), "diff", str(repo), "--from", "main", "--to", "feature",
                        "--out", str(out), "--fail-on-findings"], capture_output=True, text=True, env=env, timeout=300)
    assert p.returncode == 2, p.stderr  # there are findings
    assert "## Check first" in p.stdout
    assert (out / "diff.md").read_text().startswith("# Changes in app")
    hist = json.loads((out / "history.json").read_text())
    assert hist["entries"][0]["kind"] == "range"
    assert "__REPO_GRAPH_PAYLOAD__" in (out / "graph.html").read_text()


async def test_review_changes_tool(repo: Path):
    from mcp.client import Client
    from mcp.client.extension import advertise
    from mcp.server.apps import APP_MIME_TYPE, EXTENSION_ID

    from repo_graph_mcp.server import VIEW_URI, mcp

    async with Client(mcp, extensions=[advertise(EXTENSION_ID, {"mimeTypes": [APP_MIME_TYPE]})]) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
        assert tools["review_changes"].meta["ui"]["resourceUri"] == VIEW_URI
        r = await c.call_tool("review_changes", {"repo": str(repo), "base": "main", "head": "feature"})
        assert not r.is_error, r.content
        text = r.content[0].text
        assert "## Check first" in text and "`old_fn()` was removed" in text
        sc = r.structured_content
        assert sc["history"]["entries"][0]["kind"] == "range"
        r = await c.call_tool("explore_graph", {"graph": sc["graphId"], "node": "ask"})
        assert not r.is_error
        r = await c.call_tool("review_changes", {"repo": str(repo), "base": "nope"})
        assert r.is_error and "nope" in r.content[0].text


def test_signature_compatibility():
    assert C.compatible("f(a, b)", "f(a, b, c=None)")
    assert C.compatible("diff(old: G, new: G) -> dict", "diff(old: G, new: G, limit: int | None = MAX) -> dict")
    assert C.compatible("f(a)", "f(a, *args, **kw)")
    assert C.compatible("f(x: dict[str, int] = {})", "f(x: dict[str, int] = {}, y=1)")
    assert not C.compatible("f(a)", "f(a, b)")  # a new required parameter
    assert not C.compatible("f(a, b)", "f(b, a)")  # reordered
    assert not C.compatible("f(a, b=1)", "f(a, b)")  # a default dropped
    assert not C.compatible("f(a, b)", "f(a)")  # a parameter removed
    assert not C.compatible("f(a, very_long…", "f(a, very_long…, c=1)")  # cut short: can't tell


def test_compatible_signature_change_is_not_a_finding(repo: Path, tmp_path: Path):
    sh(repo, "checkout", "-q", "main")
    sh(repo, "checkout", "-q", "-b", "gentle")
    write(repo, "app/util.py", (repo / "app/util.py").read_text().replace("def helper(a):", "def helper(a, b=0):"))
    sh(repo, "commit", "-qam", "optional b")
    g, rep, hist = run(repo, tmp_path, head="gentle")
    (sig,) = rep["signature_changes"]
    assert sig["compatible"] and [c["n"] for c in sig["callers"]] == ["run"]
    assert not any("signature" in c for c in rep["check_first"])
    assert "backward compatible" in C.to_markdown(rep)
