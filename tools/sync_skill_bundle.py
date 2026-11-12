"""Copy the parser + view from src/repo_graph_mcp into the graph-repo skill.

Agent mode runs skills/graph-repo/scripts/graph_repo.py with no package installed, so the
skill carries its own copy of the modules it needs. Run this after changing any of them;
tests/test_skill_bundle.py fails when the copy is stale.

    python tools/sync_skill_bundle.py
"""

from __future__ import annotations

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "repo_graph_mcp"
DST = ROOT / "skills" / "graph-repo" / "scripts" / "repo_graph_mcp"
FILES = [
    "__init__.py", "graph.py", "classify.py", "builder.py", "genai.py", "parsecache.py", "history.py", "live.py", "review.py", "apprunner.py", "compare.py", "store.py", "summary.py", "fetch.py",
    "parsers/__init__.py", "parsers/base.py", "parsers/python_parser.py", "parsers/js_parser.py",
    "ui/graph-view.html",
]


def main() -> None:
    if DST.exists():
        shutil.rmtree(DST)
    for rel in FILES:
        target = DST / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(SRC / rel, target)
    print(f"synced {len(FILES)} files into {DST.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
