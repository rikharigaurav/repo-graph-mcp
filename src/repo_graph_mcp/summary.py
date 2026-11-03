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

    lines += _genai_section(g)

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


def _genai_section(g: CodeGraph) -> list[str]:
    """Agents, MCP servers, services and env vars, with where they live, for the model's context."""
    by = {lbl: [n for n in g.nodes.values() if n.label == lbl] for lbl in (G.AGENT, G.MCP_SERVER, G.SERVICE, G.ENV_VAR)}
    if not any(by.values()):
        return []
    out = ["", "GenAI & services (static scan; every link cites file:line, see explore_graph):"]
    for a in sorted(by[G.AGENT], key=lambda n: (n.path, n.props.get("line", 0))):
        steps = sorted((g.nodes[s] for s, r in g.in_adj.get(a.id, []) if r == G.BELONGS_TO
                        and g.nodes[s].label == G.AGENT_STEP), key=lambda n: n.props.get("order", 0))
        flow = a.props.get("flow") or []
        chain = _chain(g, steps)
        desc = chain or "; ".join(flow[:6]) or ", ".join(st.name for st in steps)
        tools = sorted({g.nodes[t].props.get("tool_name") or g.nodes[t].name
                        for x in [a.id, *(st.id for st in steps)] for t, r in g.out_adj.get(x, []) if r == G.USES_TOOL})
        out.append(f"  Agent {a.name} [{a.props.get('framework', '')}] {a.path}:{a.props.get('line', '?')}"
                   + (f" — {desc}" if desc else "") + (f"; tools: {', '.join(tools[:10])}" if tools else ""))
        if a.props.get("unresolved"):
            out.append(f"    not resolved statically: {'; '.join(a.props['unresolved'][:3])}")
    for m in by[G.MCP_SERVER]:
        exposed = [g.nodes[t] for t, r in g.out_adj.get(m.id, []) if r == G.EXPOSES]
        names = [n.props.get("tool_name") or n.name for n in exposed] or m.props.get("tool_names", [])
        out.append(f"  MCP server {m.name} ({m.path}{', ' + m.props['transport'] if m.props.get('transport') else ''})"
                   + (f" — exposes: {', '.join(names[:12])}" if names else ""))
    if by[G.SERVICE]:
        def sites(n):
            return sum(1 for _, r in g.in_adj.get(n.id, []) if r == G.USES)
        svcs = sorted(by[G.SERVICE], key=lambda n: -sites(n))
        out.append("  Services: " + " · ".join(f"{n.name} [{n.props.get('category', '')}] {sites(n)} use site"
                                              f"{'' if sites(n) == 1 else 's'}" for n in svcs[:12]))
    if by[G.ENV_VAR]:
        env = by[G.ENV_VAR]
        req = [n.name for n in env if n.props.get("mode") == "required"]
        out.append(f"  Env vars: {len(env)} read in code" + (f" ({len(req)} required: the app fails without them)" if req else "") + (": " + ", ".join(sorted(n.name for n in env)[:20])
                                                                                  + (" …" if len(env) > 20 else "") if env else ""))
        for n in env:
            if n.props.get("warning"):
                where = next((f"{p.get('file')}:{p.get('line')}" for (s_, t, r), p in g._edges.items()
                              if t == n.id and r == G.READS_ENV), "")
                out.append(f"  Warning: {n.name} ({where}) — {n.props['warning']}")
    skipped = (g.stats.get("genai") or {}).get("files_skipped_nonprod")
    if skipped:
        out.append(f"  ({skipped} files under tests/docs/examples/migrations were not scanned for these.)")
    return out


def _chain(g: CodeGraph, steps: list) -> str:
    """`a → b → c → END` when the agent is a straight line."""
    if not steps:
        return ""
    ids = {s.id for s in steps}
    nxt: dict[str, list[str]] = {s.id: [t for t, r in g.out_adj.get(s.id, []) if r == G.NEXT and t in ids] for s in steps}
    plain = all(g._edges[(s.id, t, G.NEXT)].get("kind") == "edge" for s in steps for t in nxt[s.id])
    if not plain or any(len(v) > 1 for v in nxt.values()) or any(s.props.get("kind") == "tools" for s in steps):
        return ""
    entry = [s for s in steps if s.props.get("entry")]
    if len(entry) != 1:
        return ""
    seq, cur, seen = [], entry[0].id, set()
    while cur and cur not in seen:
        seen.add(cur)
        seq.append(g.nodes[cur].name)
        cur = nxt[cur][0] if nxt[cur] else None
    if cur is not None or len(seq) != len(steps):  # loops back, or doesn't reach every step
        return ""
    last = next(s for s in steps if s.name == seq[-1])
    return " → ".join(seq) + (" → END" if last.props.get("can_end") else "")


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

