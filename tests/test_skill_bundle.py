"""Agent mode: the skill's bundled parser must match the package, and the CLI must work."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from sync_skill_bundle import DST, FILES, SRC  # noqa: E402

SCRIPT = ROOT / "skills" / "graph-repo" / "scripts" / "graph_repo.py"
SAMPLE = ROOT / "tests" / "fixtures" / "sample_repo"


@pytest.mark.parametrize("rel", FILES)
def test_bundle_in_sync(rel):
    assert (DST / rel).read_bytes() == (SRC / rel).read_bytes(), \
        f"{rel} is stale in the skill bundle; run python tools/sync_skill_bundle.py"


def run(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, timeout=120)


def test_cli_build_explore_search(tmp_path):
    out = tmp_path / "g"
    r = run("build", str(SAMPLE), "--out", str(out))
    assert r.returncode == 0, r.stderr
    assert "API endpoints (3)" in r.stdout and "graph_id" not in r.stdout
    html = (out / "graph.html").read_text("utf8")
    assert "window.__REPO_GRAPH_PAYLOAD__=" in html and "<title>sample_repo graph</title>" in html
    artifact = (out / "graph-artifact.html").read_text("utf8")
    assert artifact.startswith("<title>sample_repo graph</title>")
    assert "<!doctype" not in artifact.lower() and "<body" not in artifact and "__REPO_GRAPH_PAYLOAD__" in artifact
    data = json.loads((out / "graph.json").read_text("utf8"))
    assert any(n["label"] == "ApiEndpoint" for n in data["nodes"])

    r = run("explore", str(out), "normalize", "--direction", "in")
    assert r.returncode == 0, r.stderr
    assert "<-[CALLS]-" in r.stdout and (out / "explore-normalize.html").exists()
    focused = (out / "explore-normalize.html").read_text("utf8")
    assert '"focusDepth":1' in focused

    r = run("search", str(out), "UserService")
    assert "services.py" in r.stdout

    r = run("explore", str(out), "no_such_thing_xyz")
    assert r.returncode != 0 and "No node matching" in r.stderr
