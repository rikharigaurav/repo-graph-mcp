---
name: graph-repo
description: >
  This skill should be used when the user shares a code repository (a GitHub URL, "owner/repo", a git URL
  or a local folder) and asks to "graph this repo", "map the codebase", "show the structure", "visualize the
  architecture", "how is this repo organised", "what calls X", "which tests cover Y", "where is endpoint Z
  handled", "what does the agent do", "which code uses the LLM / vector DB", "what env vars does it need",
  or invokes /graph-repo. It builds a structural graph of the code, including its AI agents, MCP servers,
  external services and env vars, and shows it interactively. Also use it when the user wants to "watch
  the graph live", "see the changes as Claude edits", "show the live graph" of a local folder, "run the
  app / frontend here", or "review and accept or undo Claude's changes". And use it to review changes:
  "review this PR", "review PR #12", "what did this branch change", "what could this break", "check the
  code Claude / Cursor / a vibe-coding session wrote", "which tests should I run", "what changed since
  <commit>".
metadata:
  version: "0.8.0"
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
exactly as the user gave it, `ref` for a branch/tag, `path` for one sub-folder. To review a PR or a set of
changes, use `review_changes` when it is listed (see "Review changes" below; it takes `pr`, `base`,
`head`), else agent mode's `diff`.

The in-chat view only appears where the app can display MCP App views. When these tools reach you from a
cloud session through the user's linked computer (their names start with `mcp__remote-devices__`), or
the user says they can't see the graph, also run agent mode (B) and publish the artifact, so they always
get a view they can open.

Graph exactly the repository the user named. Never fall back to another repository (one from an
example, an earlier chat or a connected folder); if they didn't name one, ask which.

## B. Agent mode (default, no server needed)

The builder ships with this skill: `scripts/graph_repo.py` in this skill's base directory. It needs
Python 3.10+ and, for remote repos, git. On first use it installs tree-sitter into a private environment in
the user's cache folder (never into their global Python); later runs start immediately.

1. **Find Python.** Try `python3 --version`, then `python --version`, then `py -3 --version` (Windows).
   Use whichever prints 3.10 or newer as `PY` below.

2. **Build.** Quote paths that contain spaces.

   ```
   PY "<skill dir>/scripts/graph_repo.py" build <repo> [--ref <branch>] [--path <sub-folder>] [--out <dir>] [--history N]
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
     already has its title, light and dark themes, and phone layout. When you graph the same repo again,
     publish the same file path so the artifact's link stays the same instead of making a new one.
   - Otherwise, if you are running on the user's own computer, open `graph.html` in their browser:
     `start "" "<path>"` on Windows, `open "<path>"` on macOS, `xdg-open "<path>"` on Linux. Tell the
     user the file path too.
   - In a cloud container with no Artifact tool, a browser you open is invisible to the user: don't
     claim the graph is open. Hand the path of `graph-artifact.html` back (to the user, or to the
     conversation that called you) so it can be published.

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

## Version control

In a git checkout the summary has a `Version control:` block: branch and commit, how many commits aren't
pushed, the **uncommitted changes** as graph changes (functions added/changed/removed, new endpoints,
services, env vars), and the last 8 commits (`--history N`, `0` for none) the same way. The view's header
chip opens the same timeline. Use it when the user asks what changed, what a commit did, what isn't
pushed, or to review work in progress, including edits you just made: run `build` again after editing.
It's fast (snapshots per commit SHA and parses per file content are cached), so rebuild freely and
republish the page to show the latest state.

## Review changes: a PR, a branch, or work in progress

To review what a change did (a pull request, a branch, a range of commits, or uncommitted work, such as
code you or another agent just wrote), compare the two versions' graphs:

```
PY "<skill dir>/scripts/graph_repo.py" diff <repo> --pr 12                     # a GitHub pull request
PY "<skill dir>/scripts/graph_repo.py" diff <repo> --to feature [--from main]  # a branch
PY "<skill dir>/scripts/graph_repo.py" diff <repo> --from <sha> --to <sha>     # any two commits
PY "<skill dir>/scripts/graph_repo.py" diff "<local folder>" [--from HEAD]     # uncommitted work
```

Defaults: a local folder compares `--from HEAD` with what is on disk (`--to working`); a branch or PR
is compared with where it branched off its base. Pass `--out` into your working directory when you will
publish the view. It prints a Markdown review (also saved as `diff.md`, with `diff.json`) and writes a
`graph.html` / `graph-artifact.html` of the new side that opens with the changes marked and listed.

How to use the review:
- **Lead with "Check first".** Those are the findings most likely to be real breakage or real risk:
  callers in files the change didn't touch that still call a removed or moved function, or a function
  whose signature changed; secrets an env var name would expose to the browser; new packages (check
  each exists and is the one meant: a misspelt or invented package name is a classic vibe-coding
  mistake); new services and env vars that need configuring; code no test reaches.
- **Then read the diff where it points.** The review is static (calls matched by name, no runtime), so
  confirm each finding in the code before calling it a bug, and say which ones you confirmed.
- **"Tests to run"** lists the test files that call the changed code directly or within two calls.
  Run them when you can.
- Publish `graph-artifact.html` like step 3 above when the user wants to see it, and `explore` the
  output folder to look around any finding.
- `--fail-on-findings` exits with status 2 when "Check first" isn't empty, for CI.

To post a review on a GitHub PR, draft it from the findings and show it to the user first; post only
when they say so.

## Live view

When the user wants to watch the graph change while code is being edited (by you or by them), start the
live server for their **local** folder. It keeps running, so start it in the background (in Claude Code:
the Bash tool with `run_in_background`), never in the foreground:

```
PY "<skill dir>/scripts/graph_repo.py" live "<folder>" --open
```

It prints `Live view: http://127.0.0.1:7333/` (another port if that one is taken) and `--open` opens it in
the default browser. Give the user that URL and tell them to keep the page open on its **Live** tab:
each change shows up there within a second or two, with what it added, changed and removed, and the
overview rows it touched light up. Your own Edit/Write changes are labelled "Claude Code" through this
plugin's hook; nothing else is needed. Running the same command again reuses a server that's already
watching that folder. Stop it with `PY "<skill dir>/scripts/graph_repo.py" live "<folder>" --stop`.

It only watches folders on the machine where it runs: for a GitHub URL, clone it first or use `build`.

### UI tab: the app itself, and accepting or undoing changes

When the user wants to see the app (not the graph) change as you edit it, or to accept or reject your
changes, start the live view with `--ui` and point them to its **UI** tab:

```
PY "<skill dir>/scripts/graph_repo.py" live "<folder>" --open --ui
```

It finds the frontend (a package.json `dev` script for Vite, Next.js, React, Vue, Svelte, Astro, Angular…,
or a folder with `index.html`), installs its dependencies if needed, runs it, and shows it beside a
review: each changed file with its diff and **Accept** / **Undo**. The printed line "Your app: …" says
what it found; if it found nothing, ask how they run the app and pass `--ui-cmd "<command>"` (plus
`--ui-dir <folder>` or `--ui-url <url>` when needed).

Before each user message, this plugin's hook tells you which of your changes the user undid there.
When it does, the files are back as they were: re-read them before editing again and don't redo those
changes unless the user asks.

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
