---
name: graph-repo
description: >
  This skill should be used when the user shares a code repository (a GitHub URL, "owner/repo", a git URL)
  and asks to "graph this repo", "map the codebase", "show the structure", "visualize the architecture",
  "how is this repo organised", "what calls X", "which tests cover Y", "where is endpoint Z handled", or
  invokes /graph-repo. It uses the repo-graph MCP tools to build a structural graph and show it in the chat.
metadata:
  version: "0.1.0"
---

# Graph a repository

The `repo-graph` MCP server turns a repository into a code-structure graph and renders it as an
interactive view inside the conversation. Building it takes seconds: files are parsed with tree-sitter,
with no LLM summaries and no embeddings.

## Workflow

1. **Build the graph.** Call `graph_repo` with the repository exactly as the user gave it (full GitHub
   URL, `owner/repo`, or a `/tree/<branch>/<folder>` link). Pass `ref` for a branch, tag or commit and
   `path` to graph one sub-folder of a monorepo. If arguments were given with `/graph-repo`, treat them as
   the repository (and optional branch).
2. **Let the view do the drawing.** The user already sees the interactive graph. Do not re-draw it as
   Mermaid, ASCII or a table. Reply with a short orientation built from the returned summary: the main
   areas (top-level folders), entry points and API endpoints, the most-called functions and most-imported
   files, and anything notable such as frontend calls that hit backend routes or code with no tests.
   Keep the `graph_id` from the summary for follow-up calls.
3. **Drill in on request.** For "what calls X", "what does this file depend on", "which tests cover this
   endpoint": call `explore_graph` with the `graph_id` and the node (name, `Class.method`, file path, or
   `"POST /route"`). Use `direction: "in"` for callers/importers, `"out"` for dependencies, and
   `relations: ["CALLS"]` or `["IMPORTS"]` to narrow it. The tool shows a focused view; describe it.
4. **Look things up.** When unsure of a name, call `search_graph` first and pass the returned id to
   `explore_graph`.
5. **Persist if asked.** If the user wants the graph in their Neo4j database and the
   `push_graph_to_neo4j` tool is available, call it with the `graph_id`.

## Reading the graph

- Node labels: `Folder`, `DataFile` (source), `TestingFile`, `DocumentationFile`, `TemplateMarkupFile`,
  `Class`, `Function`, `ApiEndpoint` (route handlers appear as endpoints rather than as functions).
- Relationships: `BELONGS_TO` (containment), `IMPORTS`, `CALLS`, `EXTENDS`, `TEST` (a test file
  exercises a function or endpoint), and `GET`/`POST`/… (client code requesting an endpoint; the
  `match` property says whether it was exact or went through a proxy prefix such as `/api`).
- Endpoints marked "called but not defined here" are requests to routes outside the repository.
- Only Python and JavaScript/TypeScript are parsed into symbols; other languages appear as files.
  Calls are resolved statically, so say so when the user asks about dynamic dispatch, plugins or
  reflection-heavy code.

## When something fails

- "couldn't find" on a GitHub repo usually means it is private: the server needs a `GITHUB_TOKEN`.
- Rate-limit errors: suggest setting `GITHUB_TOKEN`; the server falls back to `git clone` when it can.
- Very large repos open as an overview (some kinds hidden, deep folders folded). Suggest `path` to
  focus on one package, or tell the user to double-click folders and files in the view to open them.
