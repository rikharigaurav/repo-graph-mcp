#!/usr/bin/env python3
"""repo-graph, agent mode: build a repository's structure graph without an MCP server.

Claude runs this with its own shell/code tools. It needs only Python >= 3.10; tree-sitter is
installed on first use into a private virtualenv (never into your global Python).

    python graph_repo.py build   <repo> [--ref REF] [--path SUBDIR] [--out DIR] [--history N]
    python graph_repo.py explore <DIR> <node> [--depth N] [--direction in|out|both] [--relations CALLS,IMPORTS]
    python graph_repo.py search  <DIR> <query> [--label LABEL] [--limit N]
    python graph_repo.py live    <folder> [--open] [--ui] [--ui-cmd CMD] [--port 7333] [--stop]

<repo> is a GitHub URL (incl. /tree/<branch>/<dir> links), owner/repo, any git URL, or a local path.
`build` writes DIR/graph.html (interactive, self-contained), DIR/graph.json and DIR/summary.txt and prints
the summary. In a git checkout it also shows uncommitted changes and the last N commits as graph changes;
each commit's graph is cached by its SHA, so rebuilding after an edit only re-parses the files that changed. `explore` writes DIR/explore-<node>.html focused on one node and prints its relationships.
`live` serves the view on http://127.0.0.1:7333/ and keeps it current while files change; its Live tab
lists each change as it happens (labelled with the Claude Code tool that made it, through the plugin's hook).
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


def render_page(payload: dict | bytes, title: str) -> str:
    """The view as one HTML page with the graph inside it."""
    from repo_graph_mcp import __file__ as pkg_file

    template = (Path(pkg_file).parent / "ui" / "graph-view.html").read_text("utf8")
    raw = payload.decode("utf8") if isinstance(payload, bytes) else json.dumps(payload, separators=(",", ":"))
    data = raw.replace("</", "<\\/").replace("<!--", "<\\!--")
    inject = f"<script>window.__REPO_GRAPH_PAYLOAD__={data};</script>\n<script>"
    html = template.replace("<script>", inject, 1)
    return re.sub(r"<title>.*?</title>", lambda _m: f"<title>{title.replace('<', '')}</title>", html, count=1)


def write_view(payload: dict, title: str, dest: Path) -> None:
    html = render_page(payload, title)
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
    from repo_graph_mcp import history as H
    from repo_graph_mcp.builder import build_graph
    from repo_graph_mcp.parsecache import ParseCache
    from repo_graph_mcp.store import GraphStore
    from repo_graph_mcp.summary import summarize

    t0 = time.perf_counter()
    spec = F.parse_repo_spec(args.repo, args.ref, args.path)
    commits = max(args.history, 0)
    home = cache_dir()
    snapshots = H.Snapshots(GraphStore(max_items=4, disk_dir=str(home / "snapshots")))
    sub = spec.subpath.strip("/")

    def key_for(sha: str) -> str:
        base = spec.display if spec.kind != "local" else f"local:{Path(spec.local_path).resolve().name}"
        return f"{base}@{sha[:12]}" + (f":{sub}" if sub else "")

    parse_cache = ParseCache(home / "parses" / f"{slug(spec.display if spec.kind != 'local' else str(Path(spec.local_path).resolve()))}.json.gz")
    commit_url = (lambda sha: f"{spec.url}/commit/{sha}") if spec.kind == "github" else None
    cached = False
    work = None
    co = None
    hist = None
    try:
        if spec.kind == "local":
            co = F.local_checkout(spec)
        else:
            git_spec = spec if spec.kind == "git" else F.as_git(spec)
            # the newest commit already built? then nothing needs downloading
            head = F.remote_head(git_spec) if commits else None
            spec.ref, spec.subpath = git_spec.ref, git_spec.subpath
            sub = spec.subpath.strip("/")
            hist_file = home / "history" / f"{H._safe(key_for(head))}-{commits}.json" if head else None
            snap = snapshots.get(key_for(head)) if head else None
            saved = H._read_json(hist_file) if snap is not None else None
            if snap is not None and saved:
                g, hist, cached = snap, saved, True
            else:
                # git clone uses your own git credentials, so private repos work wherever git can see them
                work = F.new_work_dir()
                print(f"repo-graph: cloning {spec.display} …", file=sys.stderr)
                co = F.clone_git(git_spec, work, depth=commits + 1 if commits else 1)
                spec.ref, spec.subpath = git_spec.ref, git_spec.subpath  # /tree/ links are re-split against real branches
                sub = spec.subpath.strip("/")
        if not cached:
            if not co.root.is_dir():
                raise F.FetchError(f"Path '{spec.subpath}' not found in {spec.display}.")
            key = key_for(co.sha)
            info = {"key": key, "display": spec.display, "name": spec.name or spec.display, "owner": spec.owner,
                    "ref": co.ref, "sha": co.sha, "source": spec.kind, "subpath": sub, "built_at": int(time.time())}
            if spec.kind == "github":
                info["url"] = spec.url
                info["blob_base"] = f"{spec.url}/blob/{co.sha}/" + (f"{sub}/" if sub else "")

            def build(folder: Path, inf: dict):
                return build_graph(folder, inf, parse_cache=parse_cache)

            if commits and H.is_git(co.root):
                g, hist = H.build_history(co.root, info, key_for, build, snapshots, commits, commit_url,
                                          log_fn=lambda m: print(f"repo-graph: {m}", file=sys.stderr),
                                          cache_dir=home / "history")
                if hist and spec.kind != "local" and not hist["dirty"]:
                    H._write_json(home / "history" / f"{H._safe(key_for(hist['head']))}-{commits}.json", hist)
            else:
                g = build(co.root, info)
            parse_cache.save()
    finally:
        if work and co:
            co.cleanup()
    secs = time.perf_counter() - t0
    key = g.repo.get("key") or key_for(g.repo.get("sha", "local"))

    out = Path(args.out) if args.out else home / "graphs" / slug(key)
    out.mkdir(parents=True, exist_ok=True)
    (out / "graph.json").write_text(json.dumps(g.to_json(), separators=(",", ":")), "utf8")
    if hist:
        (out / "history.json").write_text(json.dumps(hist, separators=(",", ":")), "utf8")
    elif (out / "history.json").exists():
        (out / "history.json").unlink()
    payload = g.to_ui_payload(max_nodes=args.max_nodes)
    payload.update(graphId=key, mode="repo", **({"history": hist} if hist else {}))
    title = f"{spec.display.split('/')[-1]} graph"
    write_view(payload, title, out / "graph.html")
    summary = summarize(g, None, secs, ui_shown=False, cached=cached, history=hist)
    (out / "summary.txt").write_text(summary, "utf8")
    print(summary)
    print(f"\nInteractive graph: {out / 'graph.html'}")
    print(f"Graph data:        {out / 'graph.json'}")
    print(f"Follow-ups:        python {Path(__file__).name} explore \"{out}\" <node>   |   search \"{out}\" <query>")


def local_rebuild(folder: Path, commits: int, home: Path, parse_cache):
    """A function that graphs `folder` as it is on disk now, with its version-control history."""
    from repo_graph_mcp import fetch as F
    from repo_graph_mcp import history as H
    from repo_graph_mcp.builder import build_graph
    from repo_graph_mcp.store import GraphStore

    spec = F.parse_repo_spec(str(folder))
    snapshots = H.Snapshots(GraphStore(max_items=4, disk_dir=str(home / "snapshots")))
    git = commits > 0 and H.is_git(folder)

    def key_for(sha: str) -> str:
        return f"local:{folder.name}@{sha[:12]}"

    def build(root: Path, info: dict):
        return build_graph(root, info, parse_cache=parse_cache)

    def rebuild():
        co = F.local_checkout(spec)
        info = {"key": key_for(co.sha), "display": spec.display, "name": spec.name or folder.name, "owner": spec.owner,
                "ref": co.ref, "sha": co.sha, "source": "local", "subpath": "", "built_at": int(time.time())}
        if git:
            return H.build_history(co.root, info, key_for, build, snapshots, commits, None, cache_dir=home / "history")
        return build(co.root, info), None

    return rebuild


def cmd_live(args) -> None:
    from repo_graph_mcp import live as L
    from repo_graph_mcp.parsecache import ParseCache

    home = cache_dir()
    folder = Path(args.folder).expanduser().resolve()
    if not folder.is_dir():
        sys.exit(f"repo-graph: {folder} isn't a folder. The live view watches a local checkout.")
    running = L.find_running(home, folder)
    if args.stop:
        if running and L.stop_running(running):
            print(f"repo-graph: stopped the live view of {folder}")
        else:
            print(f"repo-graph: no live view is running for {folder}")
        return
    if running:
        print(f"Live view of {folder} is already running: {running['url']}")
        if args.open:
            import webbrowser
            webbrowser.open(running["url"])
        return

    parse_cache = ParseCache(home / "parses" / f"{slug(str(folder))}.json.gz")
    print(f"repo-graph: graphing {folder} …", file=sys.stderr)
    t0 = time.perf_counter()
    session = L.LiveSession(folder, local_rebuild(folder, max(args.history, 0), home, parse_cache),
                            interval=args.interval, max_nodes=args.max_nodes, save=parse_cache.save)
    parse_cache.save()  # the first build parsed everything: keep it for next time
    from repo_graph_mcp.apprunner import AppRunner

    app = AppRunner(folder, cmd=args.ui_cmd, cwd=args.ui_dir, url=args.ui_url)
    server = L.LiveServer(session, lambda payload: render_page(payload, f"{folder.name} · live graph"),
                          graph_id=f"live:{folder.name}", port=args.port, home=home, idle_minutes=args.idle_minutes,
                          app=app, start_app=args.ui)
    g = session.graph
    print(f"repo-graph: {len(g.nodes):,} nodes, {g.edge_count:,} links in {time.perf_counter() - t0:.1f} s")
    print(f"\nLive view: {server.url}")
    print(f"Watching {folder} for changes (every {session.interval:g} s). Open the Live tab to follow them.")
    if app.info:
        where = f" in {app.info['dir']}/" if app.info.get("dir") else ""
        how = f"`{app.info['cmd']}`" if app.info.get("cmd") else "a static server"
        when = "starting now" if args.ui else "start it from the UI tab"
        print(f"Your app: {app.info['framework']}{where}, run with {how} ({when}).")
    else:
        print("No frontend found to preview: the UI tab still reviews changes. Use --ui-cmd to run one.")
    print(f"Stop it with Ctrl+C, or: python {Path(__file__).name} live \"{folder}\" --stop", flush=True)
    if args.open:
        import webbrowser
        webbrowser.open(server.url)
    server.serve_forever()
    print("repo-graph: live view stopped", file=sys.stderr)


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
    if (out / "history.json").exists():
        payload["history"] = json.loads((out / "history.json").read_text("utf8"))
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
    b.add_argument("--history", type=int, default=8,
                   help="recent commits to show as changes (default 8; 0 = none). Uncommitted changes always show")
    e = sub.add_parser("explore", help="focus on one node of a built graph")
    e.add_argument("dir")
    e.add_argument("node")
    e.add_argument("--depth", type=int, default=1, choices=[1, 2, 3, 4])
    e.add_argument("--direction", default="both", choices=["both", "in", "out"])
    e.add_argument("--relations", help="comma-separated, e.g. CALLS,IMPORTS")
    e.add_argument("--max-nodes", type=int, default=6000)
    lv = sub.add_parser("live", help="watch a local folder and show its graph changing as files are edited")
    lv.add_argument("folder")
    lv.add_argument("--port", type=int, default=7333, help="port on 127.0.0.1 (default 7333; another if taken)")
    lv.add_argument("--open", action="store_true", help="open the view in the default browser")
    lv.add_argument("--interval", type=float, default=1.0, help="seconds between looks at the folder (default 1)")
    lv.add_argument("--history", type=int, default=8, help="recent commits in the version-control panel")
    lv.add_argument("--idle-minutes", type=float, default=120,
                    help="stop after this long with no browser tab open (default 120; 0 = never)")
    lv.add_argument("--max-nodes", type=int, default=6000)
    lv.add_argument("--stop", action="store_true", help="stop the live view of this folder")
    lv.add_argument("--ui", action="store_true", help="start the repo's frontend for the UI tab right away")
    lv.add_argument("--ui-cmd", help="command that runs the app, e.g. \"npm run dev\" or \"streamlit run app.py\"")
    lv.add_argument("--ui-dir", help="folder (inside the repo) to run it in, or to look for the frontend in")
    lv.add_argument("--ui-url", help="the app's local URL, when it isn't printed (e.g. http://localhost:3000)")
    s = sub.add_parser("search", help="find nodes by name/path")
    s.add_argument("dir")
    s.add_argument("query")
    s.add_argument("--label")
    s.add_argument("--limit", type=int, default=20)
    args = ap.parse_args()

    if args.cmd == "build" or (args.cmd == "live" and not args.stop):
        ensure_dependencies()
    sys.path.insert(0, str(HERE))  # the bundled copy of the parser
    from repo_graph_mcp.fetch import FetchError

    try:
        {"build": cmd_build, "explore": cmd_explore, "search": cmd_search, "live": cmd_live}[args.cmd](args)
    except FetchError as e:
        sys.exit(f"repo-graph: {e}")


if __name__ == "__main__":
    main()
