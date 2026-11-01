#!/usr/bin/env python3
"""repo-graph, agent mode: build a repository's structure graph without an MCP server.

Claude runs this with its own shell/code tools. It needs only Python >= 3.10; tree-sitter is
installed on first use into a private virtualenv (never into your global Python).

    python graph_repo.py build   <repo> [--ref REF] [--path SUBDIR] [--out DIR]
    python graph_repo.py explore <DIR> <node> [--depth N] [--direction in|out|both] [--relations CALLS,IMPORTS]
    python graph_repo.py search  <DIR> <query> [--label LABEL] [--limit N]

<repo> is a GitHub URL (incl. /tree/<branch>/<dir> links), owner/repo, any git URL, or a local path.
`build` writes DIR/graph.html (interactive, self-contained), DIR/graph.json and DIR/summary.txt and prints
the summary. `explore` writes DIR/explore-<node>.html focused on one node and prints its relationships.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import venv
from pathlib import Path

HERE = Path(__file__).resolve().parent
REQUIREMENTS = [
    "tree-sitter>=0.25,<0.27",
    "tree-sitter-python>=0.25",
    "tree-sitter-javascript>=0.25",
    "tree-sitter-typescript>=0.23",
]


def cache_dir() -> Path:
    base = os.getenv("REPO_GRAPH_HOME")
    if base:
        return Path(base)
    if os.name == "nt":
        return Path(os.getenv("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "repo-graph"
    return Path(os.getenv("XDG_CACHE_HOME", Path.home() / ".cache")) / "repo-graph"


def ensure_dependencies() -> None:
    """Import tree-sitter, or install it into a private venv and re-run this script there."""
    try:
        import tree_sitter  # noqa: F401
        import tree_sitter_javascript  # noqa: F401
        import tree_sitter_python  # noqa: F401
        import tree_sitter_typescript  # noqa: F401
        return
    except ImportError:
        pass
    if os.environ.get("REPO_GRAPH_BOOTSTRAPPED"):
        sys.exit("repo-graph: tree-sitter is still missing after installing it; see the pip output above.")
    env_dir = cache_dir() / f"venv-py{sys.version_info.major}{sys.version_info.minor}"
    py = env_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not py.exists():
        print(f"repo-graph: first run, setting up a private environment in {env_dir} …", file=sys.stderr)
        try:
            venv.EnvBuilder(with_pip=True, clear=True).create(env_dir)
        except Exception as e:  # e.g. Debian python without ensurepip
            print(f"repo-graph: couldn't create a venv ({e}); installing with --user instead", file=sys.stderr)
            subprocess.check_call([sys.executable, "-m", "pip", "install", "--user", "-q", *REQUIREMENTS],
                                  stdout=sys.stderr)
            os.environ["REPO_GRAPH_BOOTSTRAPPED"] = "1"
            os.execv(sys.executable, [sys.executable, __file__, *sys.argv[1:]])
    subprocess.check_call([str(py), "-m", "pip", "install", "-q", "--disable-pip-version-check", *REQUIREMENTS],
                          stdout=sys.stderr)
    env = {**os.environ, "REPO_GRAPH_BOOTSTRAPPED": "1"}
    sys.exit(subprocess.call([str(py), str(Path(__file__).resolve()), *sys.argv[1:]], env=env))


def slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-")[:80] or "graph"


def write_view(payload: dict, title: str, dest: Path) -> None:
    from repo_graph_mcp import __file__ as pkg_file

    template = (Path(pkg_file).parent / "ui" / "graph-view.html").read_text("utf8")
    data = json.dumps(payload, separators=(",", ":")).replace("</", "<\\/").replace("<!--", "<\\!--")
    inject = f"<script>window.__REPO_GRAPH_PAYLOAD__={data};</script>\n<script>"
    html = template.replace("<script>", inject, 1)
    html = re.sub(r"<title>.*?</title>", f"<title>{title.replace('<', '')}</title>", html, count=1)
    dest.write_text(html, "utf8")
    # Artifact hosts add their own document skeleton (doctype, charset, viewport), so they get the
    # same page without the wrapper: title + styles + body content.
    head = re.search(r"<head>(.*?)</head>", html, re.S).group(1)
    body = re.search(r"<body>(.*)</body>", html, re.S).group(1)
    keep = "\n".join(re.findall(r"<title>.*?</title>", head, re.S)
                     + re.findall(r"<meta name=\"color-scheme\"[^>]*>|<style>.*?</style>", head, re.S))
    dest.with_name(dest.stem + "-artifact.html").write_text(keep + "\n" + body.strip() + "\n", "utf8")


def cmd_build(args) -> None:
    from repo_graph_mcp import fetch as F
    from repo_graph_mcp.builder import build_graph
    from repo_graph_mcp.summary import summarize

    t0 = time.perf_counter()
    spec = F.parse_repo_spec(args.repo, args.ref, args.path)
    work = None
    if spec.kind == "local":
        co = F.local_checkout(spec)
    else:
        # git clone uses your own git credentials, so private repos work wherever git can see them
        git_spec = spec if spec.kind == "git" else F.as_git(spec)
        work = F.new_work_dir()
        print(f"repo-graph: cloning {spec.display} …", file=sys.stderr)
        co = F.clone_git(git_spec, work)
        spec.ref, spec.subpath = git_spec.ref, git_spec.subpath  # /tree/ links are re-split against real branches
    try:
        if not co.root.is_dir():
            raise F.FetchError(f"Path '{spec.subpath}' not found in {spec.display}.")
        key = f"{spec.display}@{co.sha[:12]}" + (f":{spec.subpath.strip('/')}" if spec.subpath else "")
        info = {"key": key, "display": spec.display, "name": spec.name or spec.display, "owner": spec.owner,
                "ref": co.ref, "sha": co.sha, "source": spec.kind, "subpath": spec.subpath.strip("/"),
                "built_at": int(time.time())}
        if spec.kind == "github":
            info["url"] = spec.url
            info["blob_base"] = f"{spec.url}/blob/{co.sha}/" + (f"{info['subpath']}/" if info["subpath"] else "")
        g = build_graph(co.root, info)
    finally:
        if work:
            co.cleanup()
    secs = time.perf_counter() - t0

    out = Path(args.out) if args.out else cache_dir() / "graphs" / slug(key)
    out.mkdir(parents=True, exist_ok=True)
    (out / "graph.json").write_text(json.dumps(g.to_json(), separators=(",", ":")), "utf8")
    payload = g.to_ui_payload(max_nodes=args.max_nodes)
    payload.update(graphId=key, mode="repo")
    title = f"{spec.display.split('/')[-1]} graph"
    write_view(payload, title, out / "graph.html")
    summary = summarize(g, None, secs, ui_shown=False, cached=False)
    (out / "summary.txt").write_text(summary, "utf8")
    print(summary)
    print(f"\nInteractive graph: {out / 'graph.html'}")
    print(f"Graph data:        {out / 'graph.json'}")
    print(f"Follow-ups:        python {Path(__file__).name} explore \"{out}\" <node>   |   search \"{out}\" <query>")


def load_graph(directory: str):
    from repo_graph_mcp.graph import CodeGraph

    p = Path(directory)
    if p.is_dir():
        p = p / "graph.json"
    if not p.exists():
        sys.exit(f"repo-graph: no graph.json at {p}. Run `build` first.")
    return p.parent, CodeGraph.from_json(json.loads(p.read_text("utf8")))


def cmd_explore(args) -> None:
    out, g = load_graph(args.dir)
    node = g.resolve(args.node)
    if node is None:
        hits = g.search(args.node, limit=8)
        msg = f"No node matching '{args.node}'."
        if hits:
            msg += " Closest: " + "; ".join(f"{n.label} {n.props.get('qualname', n.name)} ({n.id})" for n in hits)
        sys.exit(msg)
    rels = {r.strip().upper() for r in args.relations.split(",")} if args.relations else None
    sub = g.neighborhood(node.id, depth=args.depth, relations=rels, direction=args.direction, max_nodes=500)
    print(g.describe_node(node))
    print(f"\nNeighbourhood: {len(sub.nodes)} nodes, {sub.edge_count} edges within {args.depth} hop(s)"
          + (f" over {sorted(rels)}" if rels else "") + ".")
    payload = g.to_ui_payload(max_nodes=args.max_nodes, focus=node.id)
    payload.update(graphId=g.repo.get("key", ""), mode="repo", focusDepth=args.depth)
    dest = out / f"explore-{slug(node.props.get('qualname') or node.name)}.html"
    write_view(payload, f"{node.name} neighbourhood", dest)
    print(f"Interactive view:  {dest}")


def cmd_search(args) -> None:
    _, g = load_graph(args.dir)
    hits = g.search(args.query, label=args.label, limit=args.limit)
    if not hits:
        print(f"No matches for '{args.query}'.")
        return
    for n in hits:
        loc = n.path + (f":{n.props['line']}" if n.props.get("line") else "")
        summary = f" — {n.props['summary']}" if n.props.get("summary") else ""
        print(f"- [{n.label}] {n.props.get('qualname') or n.props.get('handler') or n.name}  {loc}  id={n.id}{summary}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="fetch and graph a repository")
    b.add_argument("repo")
    b.add_argument("--ref", help="branch or tag")
    b.add_argument("--path", help="only graph this sub-folder")
    b.add_argument("--out", help="output folder (default: a per-commit folder in the repo-graph cache)")
    b.add_argument("--max-nodes", type=int, default=6000)
    e = sub.add_parser("explore", help="focus on one node of a built graph")
    e.add_argument("dir")
    e.add_argument("node")
    e.add_argument("--depth", type=int, default=1, choices=[1, 2, 3, 4])
    e.add_argument("--direction", default="both", choices=["both", "in", "out"])
    e.add_argument("--relations", help="comma-separated, e.g. CALLS,IMPORTS")
    e.add_argument("--max-nodes", type=int, default=6000)
    s = sub.add_parser("search", help="find nodes by name/path")
    s.add_argument("dir")
    s.add_argument("query")
    s.add_argument("--label")
    s.add_argument("--limit", type=int, default=20)
    args = ap.parse_args()

    if args.cmd == "build":
        ensure_dependencies()
    sys.path.insert(0, str(HERE))  # the bundled copy of the parser
    from repo_graph_mcp.fetch import FetchError

    try:
        {"build": cmd_build, "explore": cmd_explore, "search": cmd_search}[args.cmd](args)
    except FetchError as e:
        sys.exit(f"repo-graph: {e}")


if __name__ == "__main__":
    main()
