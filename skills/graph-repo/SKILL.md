---
name: graph-repo
description: >
  This skill should be used when the user shares a code repository (a GitHub URL, "owner/repo", a git URL
  or a local folder) and asks to "graph this repo", "map the codebase", "show the structure", "visualize the
  architecture", "how is this repo organised", "what calls X", "which tests cover Y", "where is endpoint Z
  handled", "what does the agent do", "which code uses the LLM / vector DB", "what env vars does it need",
  or invokes /graph-repo. It builds a structural graph of the code, including its AI agents, MCP servers,
  external services and env vars, and shows it interactively.
metadata:
  version: "0.4.0"
---

# Graph a repository

Turn a repository into a code-structure graph (folders, files, classes, functions, API endpoints and the
calls, imports, tests and HTTP requests between them) and show it as an interactive view. A GenAI &
services layer sits beside the code: AI agents with their steps and tools, MCP servers, the services the
code calls (LLMs, vector/graph DBs, databases, queues, cloud SDKs) and the env vars it reads. Parsing is
static: no LLM summaries, no embeddings, so a typical repo takes seconds.

There are two ways to run it. Pick the first that is available.

## A. MCP tools present (`graph_repo`, `explore_graph`, `search_graph`)

Use them: `graph_repo` renders the graph inside the chat and returns a summary plus a `graph_id`;
`explore_graph` shows a focused view around one node; `search_graph` finds node ids. Pass the repository
exactly as the user gave it, `ref` for a branch/tag, `path` for one sub-folder.

## B. Agent mode (default, no server needed)

The builder ships with this skill: `scripts/graph_repo.py` in this skill's base directory. It needs
Python 3.10+ and, for remote repos, git. On first use it installs tree-sitter into a private environment in
the user's cache folder (never into their global Python); later runs start immediately.

1. **Find Python.** Try `python3 --version`, then `python --version`, then `py -3 --version` (Windows).
   Use whichever prints 3.10 or newer as `PY` below.

2. **Build.** Quote paths that contain spaces.

   ```
   PY "<skill dir>/scripts/graph_repo.py" build <repo> [--ref <branch>] [--path <sub-folder>] [--out <dir>]
   ```

   `<repo>` is what the user gave: GitHub URL (including `/tree/<branch>/<folder>` links), `owner/repo`,
   any git URL, or a local path. When you will publish the result as an artifact, pass `--out` pointing
   into your working or scratchpad directory. Otherwise leave it out: output then goes to a per-commit
   folder in the repo-graph cache. The command prints a summary followed by the paths of `graph.html`
   (self-contained interactive page), `graph.json` and `summary.txt`. It also writes
   `graph-artifact.html`, the same page without the document wrapper. The first run can take up to a
   minute while dependencies install. Tell the user that's what is happening if it is slow.

3. **Show the graph.** Don't redraw it as Mermaid, ASCII or a table, and don't rewrite the HTML:
   - If you can publish artifacts (an Artifact tool is available), publish `graph-artifact.html`
     unchanged, with a one-sentence description such as "Code-structure graph of owner/repo". It
     already has its title, light and dark themes, and phone layout.
   - Otherwise open `graph.html` in the user's browser: `start "" "<path>"` on Windows,
     `open "<path>"` on macOS, `xdg-open "<path>"` on Linux. Tell the user the file path too.

4. **Orient the user.** Reply briefly, drawing on the printed summary: the main areas (top-level folders),
   the API endpoints and who calls them, the AI agents and their flow, the services it depends on, the
   most-called functions and most-imported files, and anything notable, such as frontend calls that hit
   backend routes, code with no tests, or a `Warning:` line about a secret exposed to the browser (always
   mention those).

5. **Follow-ups** use the saved graph, so they are instant and need no network:

   ```
   PY "<skill dir>/scripts/graph_repo.py" explore <out dir> "<node>" [--direction in|out|both] [--depth 1-4] [--relations CALLS,IMPORTS]
   PY "<skill dir>/scripts/graph_repo.py" search  <out dir> "<query>" [--label Function|Class|ApiEndpoint|Agent|ExternalService|EnvVar|…]
   ```

   `<node>` can be a function or method (`create_relation`, `App.create_relation`), a file path, a folder,
   a route handler name, an endpoint (`"POST /chat/"`), an agent, a service (`Pinecone`) or an env var.
   `--direction in` lists callers and importers (for a service: the code that uses it; for an env var:
   where it's read), `out` lists dependencies. Exploring an agent prints its steps in order with the
   function each runs and where it goes next; its view opens already broken down. `explore` prints the node's relationships and writes `explore-<node>.html`
   (and `explore-<node>-artifact.html`): the whole graph, opened on that neighbourhood. Show it the
   same way as step 3 when the user wants to see it.
   If `explore` says nothing matched, run `search` and retry with the returned id.

## Reading the graph

- Node labels: `Folder`, `DataFile` (source), `TestingFile`, `DocumentationFile`, `TemplateMarkupFile`,
  `Class`, `Function`, `ApiEndpoint`. Route handlers appear as endpoints, not as functions.
- Relationships: `BELONGS_TO` (containment), `IMPORTS`, `CALLS`, `EXTENDS`, `TEST` (a test file exercises
  a function or endpoint) and `GET`/`POST`/… (client code requesting an endpoint, either exactly or
  through a proxy prefix such as `/api`).
- Endpoints marked "called but not defined here" are requests to routes outside the repository.
- GenAI layer labels: `Agent`, `AgentStep`, `McpServer`, `ExternalService`, `EnvVar`, under one `GenAI`
  hub. Relationships: `NEXT` (step → step; `kind` edge, conditional or goto), `RUNS` (step → function or
  sub-agent), `ROUTES` (step → router function), `USES_TOOL`, `EXPOSES` (MCP server → tool), `USES`
  (code → service), `READS_ENV` (code → env var). Every such link cites `file:line`; `inferred` ones
  follow a client object rather than a direct SDK call, so say so when it matters. Env vars are
  `required` (fails when unset), have a default, or are optional.
- The layer sees only code. Anything configured outside the repo (cloud console, deployed env, infra
  files) isn't in it, and docs/examples/tests/migrations aren't scanned: say "not declared in the code"
  rather than "not used".
- Only Python and JavaScript/TypeScript are parsed into symbols; other languages appear as files. Calls
  are resolved statically, so say so when the user asks about dynamic dispatch, plugins or reflection.
- The view opens on an overview (Frontend → API → Backend → AI & services, with bundled links); clicking a
  row opens just that part as a graph, and Overview / Map switches between them. When you send the user
  to a part of the graph, name the row to click (e.g. "click `utils/` in the Backend column").
- In the map: click a node for details, double-click a file to show its functions or a folder to
  fold/unfold it, double-click an agent, service, MCP server or env var to break it down into the code
  behind it, press `/` to search, toggle kinds in the legend. Large repos open as an overview.

## When something fails

- No Python 3.10+: ask the user to install Python (python.org or `winget install Python.Python.3.12`),
  or to use the hosted connector described in the plugin README.
- `git clone failed`: check the URL and branch. Private repos clone with the user's own git
  credentials, so they must be able to clone it themselves. If git is missing, ask them to install it.
- Network blocked in your environment: say the repository couldn't be downloaded from here and offer to
  graph a local copy instead (pass its folder path as `<repo>`).
- Very large repos: suggest `--path` to focus on one package.
