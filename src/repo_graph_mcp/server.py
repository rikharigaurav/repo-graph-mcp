"""repo-graph MCP server.

Share a repository with Claude → `graph_repo` downloads it, parses it into a
code-structure graph (folders, files, classes, functions, API endpoints and the
calls/imports/tests/HTTP links between them) and returns it to the chat as an
interactive MCP App view, plus a compact text summary for the model.

No LLM calls and no embeddings: this is the graph half of Read-Code-Structure,
built to finish in seconds.

Run:
    repo-graph-mcp                          # stdio (Claude Desktop / Claude Code / plugins)
    repo-graph-mcp --http --port 8000       # streamable HTTP at /mcp (claude.ai custom connector)
"""

import argparse
import asyncio
import hmac
import importlib.resources
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Annotated, Any, Literal

import anyio
import httpx
from mcp.server.apps import Apps, client_supports_apps
from mcp.server.mcpserver import Context, MCPServer
from mcp_types import CallToolResult, TextContent, ToolAnnotations
from pydantic import Field

from . import __version__
from . import fetch as F
from . import graph as G
from . import history as H
from .builder import build_graph
from .parsecache import ParseCache
from .store import GraphStore
from .summary import summarize

log = logging.getLogger("repo_graph_mcp")

VIEW_URI = "ui://repo-graph/graph-view.html"
UI_MAX_NODES = int(os.getenv("REPO_GRAPH_UI_MAX_NODES", "6000"))
HISTORY_COMMITS = int(os.getenv("REPO_GRAPH_HISTORY", "8"))  # recent commits shown as changes; 0 = off
HISTORIES: dict[str, dict[str, Any]] = {}  # graph id -> uncommitted changes and recent commits
_PARSE_CACHES: dict[str, ParseCache] = {}
BUILD_SLOTS = asyncio.Semaphore(int(os.getenv("REPO_GRAPH_MAX_CONCURRENT_BUILDS", "2")))
store = GraphStore()

INSTRUCTIONS = """\
Tools for turning a code repository into a structural graph and showing it in the chat.

- When the user shares a repo (GitHub URL, owner/repo, git URL) and wants to see, map, understand or
  explore its structure, call `graph_repo`. It renders an interactive graph for the user and returns a
  summary plus a `graph_id`. Don't redraw the graph yourself (no Mermaid/ASCII copies) — the user already
  sees it; talk about what it shows.
- To zoom in on one file, function, class or endpoint (callers, callees, imports, tests), call
  `explore_graph` — it shows a focused view of that neighbourhood.
- To find symbols by name before exploring, call `search_graph`.
- The graph also has a GenAI & services layer: AI agents (LangGraph graphs and their steps, prebuilt and
  framework agents, their tools), MCP servers and what they expose, external services the code uses
  (LLM providers, vector/graph DBs, databases, queues, cloud SDKs, known APIs) and env vars it reads. Ask
  `explore_graph` about an agent, a service (direction 'in' = the code that uses it) or an env var; each
  link cites the file:line it was found on. Infrastructure files and cloud-console settings aren't read.
- For a local folder or a git clone, the summary's `Version control:` block lists the uncommitted
  changes and the last commits as graph changes (added/changed/removed functions, endpoints, services),
  and which commits aren't pushed; the view shows them on a timeline.
- Graphs are cached per commit, so calling again for the same repo is instant.
Only Python and JavaScript/TypeScript are parsed into functions/classes/calls; other languages appear as
files. Links are resolved statically, so dynamic dispatch and reflection are not followed.
"""

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=True, idempotent_hint=True)


def _load_view_html() -> str:
    try:
        return importlib.resources.files("repo_graph_mcp").joinpath("ui/graph-view.html").read_text("utf8")
    except (FileNotFoundError, OSError):
        return ("<!doctype html><meta charset=utf-8><body style='font:14px system-ui;padding:16px'>"
                "Graph view not built. Run <code>npm run build</code> in ui-src/.</body>")


# ── graph acquisition ─────────────────────────────────────────────────────────

class Progress:
    def __init__(self, ctx: Context | None):
        self.ctx = ctx

    async def __call__(self, p: float, msg: str) -> None:
        if self.ctx is None:
            return
        try:
            await self.ctx.report_progress(p, 1.0, msg)
        except Exception:  # progress is best-effort; some clients don't send a token
            pass


def _key(spec: F.RepoSpec, sha: str) -> str:
    base = spec.display if spec.kind != "local" else f"local:{spec.display}"
    key = f"{base}@{sha[:12]}"
    return f"{key}:{spec.subpath.strip('/')}" if spec.subpath else key


def _repo_info(spec: F.RepoSpec, co: F.Checkout, key: str) -> dict[str, Any]:
    info: dict[str, Any] = {
        "key": key, "display": spec.display, "name": spec.name or spec.display, "owner": spec.owner,
        "ref": co.ref, "sha": co.sha, "source": spec.kind, "subpath": spec.subpath.strip("/"),
        "built_at": int(time.time()),
    }
    if spec.kind == "github":
        info["url"] = spec.url
        sub = f"{info['subpath']}/" if info["subpath"] else ""
        info["blob_base"] = f"{spec.url}/blob/{co.sha}/{sub}"
    elif spec.kind == "git":
        info["url"] = spec.url
    return info


async def obtain_graph(repo: str, ref: str | None, path: str | None, refresh: bool,
                       ctx: Context | None) -> tuple[str, G.CodeGraph, float, bool]:
    """Return (graph_id, graph, seconds, from_cache)."""
    progress = Progress(ctx)
    t0 = time.perf_counter()
    spec = F.parse_repo_spec(repo, ref, path)
    await progress(0.05, f"Resolving {spec.display}")

    if spec.kind == "local":
        co = F.local_checkout(spec)
        key = _key(spec, co.sha + ("-dirty" if co.ref.endswith("*") else ""))
        info = _repo_info(spec, co, key)
        g, hist = await anyio.to_thread.run_sync(lambda: _with_history(co.root, spec, info, None))
        store.put(key, g, persist=False)
        if hist:
            HISTORIES[key] = hist
        return key, g, time.perf_counter() - t0, False

    if spec.kind == "github":
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=120.0), follow_redirects=True) as client:
            try:
                sha, resolved_ref = await F.resolve_github_sha(spec, client)
            except F.FetchError as e:
                if "refused" in str(e) and F.shutil.which("git"):
                    log.info("GitHub API unavailable (%s); falling back to git clone", e)
                    return await _via_git(F.as_git(spec), spec, refresh, progress, t0)
                raise
            key = _key(spec, sha)
            if not refresh and (cached := store.get(key)):
                return key, cached, time.perf_counter() - t0, True
            async with store.lock(key):
                if not refresh and (cached := store.get(key)):
                    return key, cached, time.perf_counter() - t0, True
                async with BUILD_SLOTS:
                    await progress(0.15, f"Downloading {spec.display}@{resolved_ref}")
                    co = await F.download_github(spec, sha, resolved_ref, client, F.new_work_dir())
                    try:
                        await progress(0.3, "Parsing")
                        info = _repo_info(spec, co, key)
                        g = await anyio.to_thread.run_sync(
                            lambda: build_graph(co.root, info, extra={"skipped_large": co.skipped_large}))
                    finally:
                        co.cleanup()
                store.put(key, g)
                return key, g, time.perf_counter() - t0, False

    return await _via_git(spec, spec, refresh, progress, t0)


async def _via_git(spec: F.RepoSpec, display_spec: F.RepoSpec, refresh: bool, progress: Progress, t0: float):
    async with BUILD_SLOTS:
        await progress(0.15, f"Cloning {display_spec.display}")
        work = F.new_work_dir()
        depth = HISTORY_COMMITS + 1 if HISTORY_COMMITS else 1
        co = await anyio.to_thread.run_sync(lambda: F.clone_git(spec, work, depth=depth))
        display_spec.ref, display_spec.subpath = spec.ref, spec.subpath  # /tree/ link re-split by clone_git
        try:
            key = _key(display_spec, co.sha)
            if not refresh and (cached := store.get(key)):
                return key, cached, time.perf_counter() - t0, True
            await progress(0.3, "Parsing")
            info = _repo_info(display_spec, co, key)
            if display_spec.kind == "github":
                info["source"] = "github (git clone)"
            url = (lambda sha: f"{display_spec.url}/commit/{sha}") if display_spec.kind == "github" else None
            g, hist = await anyio.to_thread.run_sync(lambda: _with_history(co.root, display_spec, info, url))
            if hist:
                HISTORIES[key] = hist
        finally:
            co.cleanup()
    store.put(key, g)
    return key, g, time.perf_counter() - t0, False


async def find_graph(graph: str, ctx: Context | None) -> tuple[str, G.CodeGraph]:
    hit = store.find(graph)
    if hit:
        return hit
    key, g, _, _ = await obtain_graph(graph, None, None, False, ctx)  # treat it as a repo reference
    return key, g


def _with_history(root, spec: F.RepoSpec, info: dict[str, Any], commit_url) -> tuple[G.CodeGraph, dict | None]:
    """Build, adding uncommitted changes and recent commits when `root` is a git checkout."""
    cache_base = os.getenv("REPO_GRAPH_CACHE_DIR")
    pc_key = spec.display if spec.kind != "local" else str(spec.local_path)
    pc = _PARSE_CACHES.get(pc_key)
    if pc is None:
        path = Path(cache_base) / "parses" / (re.sub(r"[^A-Za-z0-9_.@-]+", "_", pc_key)[:120] + ".json.gz") if cache_base else None
        pc = _PARSE_CACHES[pc_key] = ParseCache(path)

    def build(folder, inf):
        return build_graph(folder, inf, parse_cache=pc)

    try:
        if HISTORY_COMMITS and H.is_git(root):
            g, hist = H.build_history(root, info, lambda sha: _key(spec, sha), build, H.Snapshots(store),
                                      HISTORY_COMMITS, commit_url,
                                      cache_dir=Path(cache_base) / "history" if cache_base else None)
            g.repo["key"] = info["key"]
            return g, hist
        return build(root, info), None
    finally:
        pc.save()


def _payload(key: str, g: G.CodeGraph, focus: str | None = None, mode: str = "repo") -> dict[str, Any]:
    p = g.to_ui_payload(max_nodes=UI_MAX_NODES, focus=focus)
    if key in HISTORIES:
        p["history"] = HISTORIES[key]
    p["graphId"] = key
    p["mode"] = mode
    return p


def _error(msg: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=msg)], structured_content={"error": msg},
                          is_error=True)


# ── tools ─────────────────────────────────────────────────────────────────────

apps = Apps()


@apps.tool(resource_uri=VIEW_URI, name="graph_repo", title="Graph a repository", annotations=READ_ONLY)
async def graph_repo(
    ctx: Context,
    repo: Annotated[str, Field(description="Repository to graph: GitHub URL (also /tree/<branch>/<dir> links), "
                                           "'owner/repo', any git URL, or an absolute path on the MCP server's machine.")],
    ref: Annotated[str | None, Field(description="Branch, tag or commit SHA. Defaults to the default branch.")] = None,
    path: Annotated[str | None, Field(description="Only graph this sub-folder (e.g. 'backend' or 'packages/api').")] = None,
    refresh: Annotated[bool, Field(description="Rebuild even if this commit is already cached.")] = False,
) -> CallToolResult:
    """Parse a code repository into a structure graph and show it to the user as an interactive view.

    Nodes: Folder, DataFile (source), TestingFile, DocumentationFile, TemplateMarkupFile, Class, Function,
    ApiEndpoint, plus the GenAI layer: Agent, AgentStep, McpServer, ExternalService, EnvVar. Edges:
    BELONGS_TO (containment), IMPORTS, CALLS, EXTENDS, TEST, GET/POST/... from HTTP client calls to the
    endpoints they hit, and NEXT / RUNS / ROUTES / USES_TOOL (agent flow and what steps run), EXPOSES (MCP
    server -> tool), USES (code -> service), READS_ENV (code -> env var). Returns a text summary (layout,
    endpoints, agents, services, env vars, hubs) and a graph_id for explore_graph / search_graph. Takes
    seconds — there are no LLM or embedding calls.
    """
    try:
        key, g, secs, cached = await obtain_graph(repo, ref, path, refresh, ctx)
    except F.FetchError as e:
        return _error(str(e))
    except httpx.HTTPError as e:
        return _error(f"Network error while fetching the repository: {e}")
    text = summarize(g, key, secs, ui_shown=client_supports_apps(ctx), cached=cached, history=HISTORIES.get(key))
    return CallToolResult(content=[TextContent(type="text", text=text)], structured_content=_payload(key, g))


@apps.tool(resource_uri=VIEW_URI, name="explore_graph", title="Explore part of a repo graph", annotations=READ_ONLY)
async def explore_graph(
    ctx: Context,
    graph: Annotated[str, Field(description="graph_id from graph_repo, or the repo (URL / owner/repo) — it is built if needed.")],
    node: Annotated[str, Field(description="Node id, or a name/path to look up: a function or method "
                                           "('create_relation', 'App.create_relation'), file path, folder, or endpoint ('POST /chat/').")],
    depth: Annotated[int, Field(ge=1, le=4, description="How many hops out from the node.")] = 1,
    relations: Annotated[list[str] | None, Field(description="Only follow these edge types, e.g. ['CALLS'] for a call "
                                                             "graph or ['IMPORTS']. Default: all.")] = None,
    direction: Annotated[Literal["both", "in", "out"], Field(description="'in' = who uses it (callers, importers), "
                                                                         "'out' = what it uses.")] = "both",
) -> CallToolResult:
    """Show the neighbourhood of one node — e.g. everything that calls a function, what a file imports,
    which tests cover an endpoint, an agent's steps and the functions they run, the code that uses a
    service or reads an env var — as a focused interactive view, with a text description."""
    try:
        key, g = await find_graph(graph, ctx)
    except (F.FetchError, httpx.HTTPError) as e:
        return _error(str(e))
    center = g.resolve(node)
    if center is None:
        hits = g.search(node, limit=8)
        hint = ("Closest matches: " + "; ".join(f"{n.label} {n.props.get('qualname', n.name)} ({n.id})" for n in hits)) if hits else ""
        return _error(f"No node matching '{node}' in {key}. {hint}".strip())
    rels = {r.upper() for r in relations} if relations else None
    sub = g.neighborhood(center.id, depth=depth, relations=rels, direction=direction, max_nodes=500)
    text = g.describe_node(center) + f"\n\nShowing {len(sub.nodes)} nodes / {sub.edge_count} edges within {depth} hop(s)" \
        + (f" over {sorted(rels)}" if rels else "") + f" (graph_id {key})."
    payload = _payload(key, sub, focus=center.id, mode="explore")
    payload["stats"] = g.stats
    return CallToolResult(content=[TextContent(type="text", text=text)], structured_content=payload)


@apps.tool(resource_uri=VIEW_URI, name="graph_view_data", title="Graph view data", visibility=["app"],
           annotations=READ_ONLY)
async def graph_view_data(
    ctx: Context,
    graph: Annotated[str, Field(description="graph_id")],
    focus: Annotated[str | None, Field(description="Node id to centre on; omit for the whole graph.")] = None,
    depth: Annotated[int, Field(ge=1, le=4)] = 2,
) -> CallToolResult:
    """Used by the in-chat view to load a graph or expand around a node. Not for the model."""
    hit = store.find(graph)
    if not hit:
        return _error(f"Graph {graph} is no longer cached on the server — ask Claude to graph the repo again.")
    key, g = hit
    if focus and focus in g.nodes:
        sub = g.neighborhood(focus, depth=depth, max_nodes=800)
        payload = _payload(key, sub, focus=focus, mode="explore")
        payload["stats"] = g.stats
    else:
        payload = _payload(key, g)
    return CallToolResult(content=[TextContent(type="text", text=f"{len(payload['nodes'])} nodes")],
                          structured_content=payload)


apps.add_html_resource(
    VIEW_URI, _load_view_html(), name="Repo graph view", title="Repository graph",
    description="Interactive force-directed view of a repository's structure graph.",
    prefers_border=True,
)

mcp = MCPServer(
    name="repo-graph",
    title="Repo Graph",
    version=__version__,
    instructions=INSTRUCTIONS,
    extensions=[apps],
)


@mcp.tool(name="search_graph", title="Search a repo graph", annotations=READ_ONLY)
async def search_graph(
    ctx: Context,
    graph: Annotated[str, Field(description="graph_id from graph_repo, or the repo (URL / owner/repo).")],
    query: Annotated[str, Field(description="Name, path fragment or docstring words to look for.")],
    label: Annotated[str | None, Field(description="Restrict to one node label: Function, Class, ApiEndpoint, "
                                                   "DataFile, TestingFile, Folder, DocumentationFile, TemplateMarkupFile, "
                                                   "Agent, AgentStep, McpServer, ExternalService, EnvVar.")] = None,
    limit: Annotated[int, Field(ge=1, le=100)] = 20,
) -> str:
    """Find nodes in a repo graph by name/path. Returns ids you can pass to explore_graph."""
    try:
        key, g = await find_graph(graph, ctx)
    except (F.FetchError, httpx.HTTPError) as e:
        return f"Error: {e}"
    hits = g.search(query, label=label, limit=limit)
    if not hits:
        return f"No matches for '{query}'" + (f" with label {label}" if label else "") + f" in {key}."
    lines = [f"{len(hits)} match(es) in {key}:"]
    for n in hits:
        loc = n.path + (f":{n.props['line']}" if n.props.get("line") else "")
        degree = len(g.in_adj.get(n.id, [])) + len(g.out_adj.get(n.id, []))
        summary = f" — {n.props['summary']}" if n.props.get("summary") else ""
        lines.append(f"- [{n.label}] {n.props.get('qualname', n.name)}  {loc}  ({degree} edges)  id={n.id}{summary}")
    return "\n".join(lines)


def _neo4j_enabled() -> bool:
    if not os.getenv("NEO4J_URI"):
        return False
    try:
        import neo4j  # noqa: F401
    except ImportError:
        log.warning("NEO4J_URI is set but the neo4j driver isn't installed: pip install 'repo-graph-mcp[neo4j]'")
        return False
    return True


if _neo4j_enabled():

    @mcp.tool(name="push_graph_to_neo4j", title="Save a repo graph to Neo4j",
              annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True,
                                          open_world_hint=True))
    async def push_graph_to_neo4j(
        ctx: Context,
        graph: Annotated[str, Field(description="graph_id from graph_repo, or the repo.")],
        replace: Annotated[bool, Field(description="Delete this repo's existing nodes first (other repos are untouched).")] = True,
    ) -> str:
        """Write the graph into the configured Neo4j database (same labels/relationships as the
        Read-Code-Structure backend, ids namespaced by repo) so the existing explorer UI and Cypher tools can use it."""
        from .neo4j_push import push_graph

        try:
            key, g = await find_graph(graph, ctx)
        except (F.FetchError, httpx.HTTPError) as e:
            return f"Error: {e}"
        counts = await anyio.to_thread.run_sync(lambda: push_graph(
            g, os.environ["NEO4J_URI"], os.getenv("NEO4J_USERNAME", "neo4j"), os.getenv("NEO4J_PASSWORD", ""),
            os.getenv("NEO4J_DATABASE") or None, replace=replace))
        repo = g.repo.get("display")
        return (f"Wrote {counts['nodes']} nodes and {counts['relationships']} relationships for {key} to Neo4j"
                f" (replaced {counts['deleted']} old nodes). Query with e.g. "
                f"MATCH (n {{repo: '{repo}'}})-[r]->(m) RETURN n, r, m LIMIT 100")


# ── HTTP hosting ──────────────────────────────────────────────────────────────

def http_app(host: str):
    from mcp.server.transport_security import TransportSecuritySettings
    from starlette.middleware.cors import CORSMiddleware
    from starlette.responses import JSONResponse, PlainTextResponse

    allowed = [h.strip() for h in os.getenv("REPO_GRAPH_ALLOWED_HOSTS", "").split(",") if h.strip()]
    security = TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=allowed,
                                         allowed_origins=["https://claude.ai", "https://*.claude.ai", *[f"https://{h}" for h in allowed]]) if allowed else None
    app = mcp.streamable_http_app(stateless_http=True, host=host, transport_security=security)
    token = os.getenv("REPO_GRAPH_AUTH_TOKEN")

    class TokenGate:
        """Optional shared-secret check: `Authorization: Bearer <token>` or `?key=<token>` on the URL
        (the query form works for clients that can't set headers, e.g. a custom connector URL)."""

        def __init__(self, inner):
            self.inner = inner

        async def __call__(self, scope, receive, send):
            if token and scope["type"] == "http" and scope.get("path") not in ("/health", "/"):
                from starlette.requests import Request

                req = Request(scope)
                supplied = req.headers.get("authorization", "").removeprefix("Bearer ").strip() or req.query_params.get("key", "")
                if not hmac.compare_digest(supplied, token):
                    await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
                    return
            await self.inner(scope, receive, send)

    async def health(_request):
        return JSONResponse({"ok": True, "version": __version__, "cached_graphs": len(store.keys())})

    async def index(_request):
        return PlainTextResponse("repo-graph MCP server. MCP endpoint: /mcp\n")

    app.add_route("/health", health)
    app.add_route("/", index)
    app.add_middleware(TokenGate)
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
                       expose_headers=["mcp-session-id"])
    return app


def main() -> None:
    ap = argparse.ArgumentParser(prog="repo-graph-mcp", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--http", action="store_true", help="serve streamable HTTP at /mcp instead of stdio")
    ap.add_argument("--host", default=os.getenv("HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.getenv("PORT", "8000")))
    ap.add_argument("--version", action="version", version=__version__)
    args = ap.parse_args()
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.http:
        # a hosted server must not read arbitrary paths on its own disk
        if os.getenv("REPO_GRAPH_ALLOW_LOCAL_PATHS") is None:
            F.ALLOW_LOCAL = False
        import uvicorn

        log.info("repo-graph MCP on http://%s:%s/mcp", args.host, args.port)
        uvicorn.run(http_app(args.host), host=args.host, port=args.port, log_level="warning")
    else:
        mcp.run("stdio")


if __name__ == "__main__":
    main()
