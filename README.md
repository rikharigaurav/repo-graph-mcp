# repo-graph-mcp

Share a repository with Claude and get its **code-structure graph back in the chat**: folders, files,
classes, functions and API endpoints, with the calls, imports, tests and HTTP requests between them.
You can pan, search, filter and click through it right in the conversation.

It's the graph half of [Read-Code-Structure](https://github.com/rikharigaurav/Read-Code-Structure),
packaged as a Claude plugin / MCP server. It makes no LLM summaries and no embeddings, so a typical
repo is graphed in a second or two instead of minutes, and it needs no API keys.

![The graph of Read-Code-Structure rendered inside the chat](docs/graph-view.png)

<table><tr>
<td><img src="docs/graph-view-details.png" alt="Selecting an endpoint shows who requests it and what it calls"></td>
<td><img src="docs/graph-view-dark.png" alt="Dark theme: a FastAPI backend and the frontend client that calls it"></td>
</tr></table>

## What it does

| | Read-Code-Structure backend | repo-graph-mcp |
|---|---|---|
| File categorising | Mistral call per folder | Deterministic rules (same categories) |
| Structure | tree-sitter (Python) | tree-sitter (Python, JavaScript, TypeScript, JSX/TSX) |
| Summaries | LLM per function/file | First line of the docstring/JSDoc |
| Vectors | Pinecone embeddings | none |
| Storage | Neo4j (required) | in-memory + disk cache per commit; Neo4j optional |
| Output | your web UI | interactive view inside Claude, plus a summary for the model |

The graph uses the same labels and relationships as the backend's Neo4j schema, so it can be pushed into
the same database. It adds a few that are free to compute:

| Nodes | |
|---|---|
| `Folder` | every folder that contains a kept file |
| `DataFile` · `TestingFile` · `DocumentationFile` · `TemplateMarkupFile` | files, classified by rules (configs, lock files, binaries, generated and vendored code are skipped) |
| `Class` | classes, with their bases |
| `Function` | functions and methods (`qualname`, signature, lines, docstring) |
| `ApiEndpoint` | FastAPI / Flask / Starlette decorators (incl. `APIRouter(prefix=…)`), Express-style `app.get('/x', …)`, Next.js `app/**/route.ts` and `pages/api`. External or unresolved routes your code calls are added too, marked "not defined in this repo" |

| Relationships | |
|---|---|
| `BELONGS_TO` | containment: function → class → file → folder |
| `IMPORTS` | file → file, incl. relative imports, `src/` layouts and tsconfig `paths` aliases like `@/` |
| `CALLS` | resolved statically: local names, imports, `self`/`this` methods incl. inherited ones, module instances (`from db import app; app.query()`), `Cls(...).method()`, React components used in JSX |
| `EXTENDS` | class → base class |
| `TEST` | test file → functions and endpoints it exercises (`client.get('/users/42')` matches `/users/{id}`) |
| `GET` · `POST` · … | client code (`fetch`, axios, requests/httpx, generated API clients) → the endpoint it hits, including through proxy prefixes like `/api` or `/api/v1` |

## Tools

| Tool | Shown to | What it does |
|---|---|---|
| `graph_repo` | model + view | Fetch and parse a repo, render the interactive graph, return a summary and a `graph_id` |
| `explore_graph` | model + view | Focused view around one node: callers, callees, imports, tests (`direction`, `depth`, `relations`) |
| `search_graph` | model | Find nodes by name/path/docstring |
| `graph_view_data` | view only | Lets the view load the whole graph from an explore result |
| `push_graph_to_neo4j` | model | Only when `NEO4J_URI` is set: write the graph into Neo4j (ids namespaced per repo) |

The graph itself goes to the view as `structuredContent`, which MCP Apps hosts keep out of the model's
context. Claude gets a compact summary instead (layout, endpoints and who calls them, most-called
functions, most-imported files, test reach) and uses `explore_graph`/`search_graph` to dig further.

In the view you can:
- click a node for its details and relationships; every entry is clickable;
- double-click a file to show its functions, or a folder to fold or unfold it;
- search with `/`, zoom with ctrl/⌘ + scroll (plain scroll once you've clicked into the map), and fit with `0`;
- toggle node and link kinds in the legend;
- use "Ask Claude about this" to send the selection back to the chat.

Large repos open as an overview: functions and docs hidden, deep folders folded. The status line says what
was folded and how to open it.

## Install

You need [uv](https://docs.astral.sh/uv/) (or Python ≥ 3.10 and pip). Set `GITHUB_TOKEN` for private
repos and higher GitHub rate limits; without it the server uses anonymous access and falls back to
`git clone` when rate-limited.

### Claude Code / Claude plugin

```
/plugin marketplace add rikharigaurav/repo-graph-mcp
/plugin install repo-graph@repo-graph-mcp
```

This adds the MCP server and a `graph-repo` skill. Then share a repo with Claude and ask for its graph, or
run `/graph-repo https://github.com/owner/repo`. In a terminal you get the text summary and exploration
tools. Apps that render MCP Apps, such as Claude on desktop and web, also show the interactive view.

### Claude Desktop (local server)

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "repo-graph": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/rikharigaurav/repo-graph-mcp", "repo-graph-mcp"],
      "env": { "GITHUB_TOKEN": "ghp_…" }
    }
  }
}
```

A local server can also graph folders on your machine: pass an absolute path as the repo.

### claude.ai (web and mobile): host it as a custom connector

Run the HTTP server somewhere public, for example on your own site:

```bash
docker build -t repo-graph-mcp .
docker run -d -p 8000:8000 -v repo-graph-cache:/data \
  -e GITHUB_TOKEN=ghp_… \
  -e REPO_GRAPH_AUTH_TOKEN="$(openssl rand -hex 24)" \
  -e REPO_GRAPH_ALLOWED_HOSTS=graph.your-site.com \
  repo-graph-mcp
```

Put it behind HTTPS, then in Claude go to **Settings → Connectors → Add custom connector** and use
`https://graph.your-site.com/mcp?key=<REPO_GRAPH_AUTH_TOKEN>`. Without Docker:
`pip install . && repo-graph-mcp --http --host 0.0.0.0 --port 8000`.

Hosted mode never reads paths on the server's own disk unless you set `REPO_GRAPH_ALLOW_LOCAL_PATHS=1`.

## Configuration

| Variable | Default | |
|---|---|---|
| `GITHUB_TOKEN` | – | Private repos, higher rate limits (`GH_TOKEN` also works) |
| `REPO_GRAPH_CACHE_DIR` | system temp | Where downloads are unpacked and graphs are cached (gzipped JSON per commit) |
| `REPO_GRAPH_MAX_FILES` | `6000` | Files graphed per repo (shallowest first) |
| `REPO_GRAPH_MAX_FILE_KB` | `768` | Larger files are skipped |
| `REPO_GRAPH_MAX_DOWNLOAD_MB` | `300` | Archive size limit |
| `REPO_GRAPH_UI_MAX_NODES` | `6000` | Nodes sent to the view (most structural first) |
| `REPO_GRAPH_MAX_CONCURRENT_BUILDS` | `2` | Parallel builds in hosted mode |
| `REPO_GRAPH_AUTH_TOKEN` | – | HTTP mode: require `Authorization: Bearer …` or `?key=…` |
| `REPO_GRAPH_ALLOWED_HOSTS` | – | HTTP mode: enable DNS-rebinding protection for these hostnames |
| `REPO_GRAPH_ALLOW_LOCAL_PATHS` | `1` stdio, `0` HTTP | Allow graphing directories on the server |
| `NEO4J_URI`, `NEO4J_USERNAME`, `NEO4J_PASSWORD`, `NEO4J_DATABASE` | – | Enables `push_graph_to_neo4j` (`pip install "repo-graph-mcp[neo4j]"`) |

## Using it with the existing Neo4j explorer

With `NEO4J_*` set, ask Claude to "save this graph to Neo4j". Nodes keep the backend's labels and property
names (`file_name`, `function_name`, `endpoint_name`, `http_method`, …) plus `repo`, so existing Cypher
keeps working. Scope it to one repo with `MATCH (n {repo: 'owner/name'})`. Re-pushing replaces only that
repo's nodes.

## How it works

1. **Fetch**: resolve the commit SHA (one small GitHub API call); on a cache hit, stop there. Otherwise
   download the tarball and extract it while skipping `node_modules`, build output, VCS folders and
   oversized files. Other git hosts use `git clone --depth 1`.
2. **Classify**: the rules from the backend's LLM prompt, applied deterministically.
3. **Parse**: tree-sitter for Python, JS, TS, JSX and TSX; one pass per file collects symbols, imports,
   call sites, routes and HTTP client calls.
4. **Resolve**: module index (Python packages, relative imports, tsconfig paths), then calls, inheritance,
   tests and HTTP links. An edge is only added when its target is actually defined in the repo; there is
   no fuzzy name matching.
5. **Deliver**: summary to the model, graph to the view, cached by commit.

Limits: only Python and JS/TS are parsed into symbols (other languages show as files). Dynamic dispatch,
dependency injection and reflection aren't followed. Route matching is static, so routes built at
runtime won't appear.

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest -q

# the in-chat view (TypeScript, bundled into one HTML file inside the Python package)
cd ui-src && npm ci && npm run build      # writes src/repo_graph_mcp/ui/graph-view.html
```

The view talks to the host using the MCP Apps protocol (`ui/initialize`, `ui/notifications/tool-result`,
`tools/call`, `ui/open-link`, `ui/message`, `ui/update-model-context`) through a small built-in bridge.
That keeps the bundle around 65 KB, where the official SDK would add about 650 KB. To try it outside Claude,
run `repo-graph-mcp --http --port 3001` and open it in the
[MCP Apps basic-host](https://github.com/modelcontextprotocol/ext-apps/tree/main/examples/basic-host).

## License

MIT
