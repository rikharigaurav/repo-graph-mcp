"""Compact text digest of a graph for the model's context.

The full graph goes to the in-chat view via `structuredContent` (which hosts
keep out of the model context); the model gets this summary instead, plus the
explore/search tools to drill in on demand.
"""

from __future__ import annotations

from collections import Counter

from . import graph as G
from .graph import CodeGraph


def _n(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def _fmt_counts(c: dict, order: tuple[str, ...] | None = None) -> str:
    items = [(k, c[k]) for k in order if c.get(k)] if order else sorted(c.items(), key=lambda kv: -kv[1])
    return " · ".join(f"{k} {v}" for k, v in items)


def summarize(g: CodeGraph, graph_id: str | None, elapsed_s: float, ui_shown: bool, cached: bool) -> str:
    """`graph_id=None` omits the MCP follow-up hint (the agent-mode CLI prints its own)."""
    s = g.stats
    labels = s.get("labels", {})
    repo = g.repo
    head = f"{repo.get('display', repo.get('name'))}"
    if repo.get("subpath"):
        head += f"/{repo['subpath']}"
    ref = repo.get("ref") or ""
    sha = (repo.get("sha") or "")[:7]
    timing = "served from cache" if cached else f"built in {elapsed_s:.1f}s (no LLM or embedding calls)"
    lines = [
        f"Graphed {head} @ {ref}{f' ({sha})' if sha and sha != 'local' else ''} — {s.get('nodes', 0)} nodes, "
        f"{s.get('edges', 0)} edges, {timing}.",
    ]
    if graph_id:
        lines.append(f"graph_id: {graph_id}   (pass to explore_graph / search_graph)")
    if ui_shown:
        lines.append("The user can see an interactive graph of this repo in the chat now; don't redraw it — "
                     "describe or explore it instead.")
    langs = s.get("languages", {})
    lines.append("")
    lines.append(
        f"Files: {labels.get(G.SOURCE_FILE, 0)} source"
        + (f" ({_fmt_counts(langs)})" if langs else "")
        + f", {_n(labels.get(G.TEST_FILE, 0), 'test')}, {_n(labels.get(G.DOC_FILE, 0), 'doc')}, "
        f"{_n(labels.get(G.TEMPLATE_FILE, 0), 'template')} in {_n(labels.get(G.FOLDER, 0), 'folder')}"
    )
    eps = [n for n in g.nodes.values() if n.label == G.ENDPOINT]
    defined = [n for n in eps if n.props.get("defined_in_repo", True)]
    lines.append(
        f"Symbols: {labels.get(G.FUNCTION, 0)} functions/methods, {labels.get(G.CLASS, 0)} classes · "
        f"API endpoints: {len(defined)} defined" + (f", {len(eps) - len(defined)} called but not defined here" if len(eps) > len(defined) else "")
    )
    lines.append("Edges: " + _fmt_counts(s.get("relationships", {})))

    # top-level layout
    top = []
    for src, rel in g.in_adj.get("FOLDER:.", []):
        if rel != G.BELONGS_TO:
            continue
        n = g.nodes[src]
        if n.label == G.FOLDER:
            top.append((n.props.get("_total_files", _count_files(g, src)), f"{n.name}/"))
        elif n.label in G.FILE_LABELS:
            top.append((0, n.name))
    top.sort(key=lambda x: (-x[0], x[1]))
    if top:
        shown = [f"{name} ({_n(cnt, 'file')})" if cnt else name for cnt, name in top[:14]]
        more = f" … +{len(top) - 14}" if len(top) > 14 else ""
        lines += ["", "Top level: " + " · ".join(shown) + more]

    if defined:
        lines += ["", f"API endpoints ({len(defined)}):"]
        for n in sorted(defined, key=lambda n: (n.props.get("route", ""), n.props.get("http_method", "")))[:25]:
            callers = [g.nodes[x] for x, r in g.in_adj.get(n.id, []) if r in G.HTTP_METHODS]
            tests = sum(1 for x, r in g.in_adj.get(n.id, []) if r == G.TEST)
            extra = []
            if callers:
                extra.append("called from " + ", ".join(sorted({c.path for c in callers if c.path}))[:120])
            if tests:
                extra.append(f"{tests} test file{'s' if tests > 1 else ''}")
            fw = n.props.get("framework")
            lines.append(f"  {n.name}  — {n.path}:{n.props.get('line', '?')}"
                         + (f" [{fw}]" if fw else "") + (f"  ({'; '.join(extra)})" if extra else ""))
        if len(defined) > 25:
            lines.append(f"  … +{len(defined) - 25} more (search_graph label=ApiEndpoint)")

    called = Counter()
    for s_, t, r, p in g.edges:
        if r == G.CALLS and g.nodes[t].label in (G.FUNCTION, G.CLASS):
            called[t] += 1
    if called:
        lines += ["", "Most-called functions:"]
        lines.append("  " + " · ".join(
            f"{g.nodes[t].props.get('qualname', g.nodes[t].name)} ({c} callers, {g.nodes[t].path})"
            for t, c in called.most_common(8)
        ))

    imported = Counter(t for s_, t, r, _ in g.edges if r == G.IMPORTS)
    if imported:
        lines += ["Most-imported files: " + " · ".join(f"{g.nodes[t].path} ({c})" for t, c in imported.most_common(6))]

    sym_per_file = Counter()
    for n in g.nodes.values():
        if n.label in (G.FUNCTION, G.CLASS):
            sym_per_file[n.path] += 1
    if sym_per_file:
        lines += ["Largest modules: " + " · ".join(f"{p} ({c} symbols)" for p, c in sym_per_file.most_common(5))]

    testable = [n for n in g.nodes.values() if n.label in (G.FUNCTION, G.ENDPOINT) and n.props.get("defined_in_repo", True)]
    if labels.get(G.TEST_FILE) and testable:
        covered = sum(1 for n in testable if any(r == G.TEST for _, r in g.in_adj.get(n.id, [])))
        lines.append(f"Directly referenced from tests: {covered}/{len(testable)} functions & endpoints.")

    skipped = [f"{s.get(k)} {label}" for k, label in (
        ("files_too_large", "too large"), ("files_minified", "minified"), ("files_failed", "unparseable"))
        if s.get(k)]
    if skipped:
        lines.append("Skipped files: " + ", ".join(skipped) + ".")
    for w in g.warnings:
        lines.append(f"Note: {w}")
    lines += ["", "Parsed languages: Python, JavaScript/JSX, TypeScript/TSX. Other source files appear as file "
                  "nodes without symbols. Edges are resolved statically (imports, self/this, module instances); "
                  "dynamic dispatch isn't followed."]
    return "\n".join(lines)


def _count_files(g: CodeGraph, folder_id: str) -> int:
    total, stack = 0, [folder_id]
    while stack:
        cur = stack.pop()
        for src, rel in g.in_adj.get(cur, []):
            if rel != G.BELONGS_TO:
                continue
            lbl = g.nodes[src].label
            if lbl == G.FOLDER:
                stack.append(src)
            elif lbl in G.FILE_LABELS:
                total += 1
    return total

