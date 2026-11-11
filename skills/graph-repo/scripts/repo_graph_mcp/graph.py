"""In-memory code-structure graph.

The node labels, id formats and relationship names deliberately mirror the
schema written by the Read-Code-Structure backend (utils/neodb.py), so a graph
built here can be pushed into the same Neo4j database and queried with the
same Cypher. Differences from that backend:

* ids use repo-relative paths instead of absolute clone paths;
* there is no LLM summary — `summary` is the first line of the docstring;
* two extra labels (`Class`) and relationships (`IMPORTS`, `EXTENDS`) are added
  because they are free to compute and make the structure much easier to read.
"""

from __future__ import annotations

from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Iterable

# ── Vocabulary ─────────────────────────────────────────────────────────────────

FOLDER = "Folder"
SOURCE_FILE = "DataFile"
TEST_FILE = "TestingFile"
DOC_FILE = "DocumentationFile"
TEMPLATE_FILE = "TemplateMarkupFile"
FUNCTION = "Function"
CLASS = "Class"
ENDPOINT = "ApiEndpoint"

# GenAI layer (genai.py): one hub node groups what the app is built on
GENAI = "GenAI"  # hub
AGENT = "Agent"  # a LangGraph graph, a prebuilt agent or a framework Agent(...)
AGENT_STEP = "AgentStep"  # one node of an agent's graph
MCP_SERVER = "McpServer"
SERVICE = "ExternalService"  # LLM provider, vector/graph DB, database, queue, cloud SDK…
ENV_GROUP = "EnvGroup"
ENV_VAR = "EnvVar"

FILE_LABELS = (SOURCE_FILE, TEST_FILE, DOC_FILE, TEMPLATE_FILE)
GENAI_LABELS = (GENAI, AGENT, AGENT_STEP, MCP_SERVER, SERVICE, ENV_GROUP, ENV_VAR)
ALL_LABELS = (FOLDER, *FILE_LABELS, CLASS, FUNCTION, ENDPOINT, *GENAI_LABELS)

BELONGS_TO = "BELONGS_TO"
CALLS = "CALLS"
IMPORTS = "IMPORTS"
TEST = "TEST"
EXTENDS = "EXTENDS"
HTTP_METHODS = ("GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS", "ANY")
NEXT = "NEXT"  # agent step -> step (props: kind edge|conditional|goto)
RUNS = "RUNS"  # agent step -> function / sub-agent it executes
ROUTES = "ROUTES"  # agent step -> router function deciding where to go next
USES_TOOL = "USES_TOOL"  # agent / step / function binding tools -> tool function
EXPOSES = "EXPOSES"  # MCP server -> tool / resource / prompt function
USES = "USES"  # code -> external service
READS_ENV = "READS_ENV"  # code -> env var
GENAI_RELS = (NEXT, RUNS, ROUTES, USES_TOOL, EXPOSES, USES, READS_ENV)


@dataclass
class Node:
    id: str
    label: str
    name: str
    path: str = ""  # repo-relative posix path of the file/folder that owns it
    props: dict[str, Any] = field(default_factory=dict)

    def to_ui(self) -> dict[str, Any]:
        d: dict[str, Any] = {"id": self.id, "l": self.label, "n": self.name}
        if self.path:
            d["p"] = self.path
        if self.props:
            d["x"] = self.props
        return d


class CodeGraph:
    def __init__(self, repo: dict[str, Any]):
        self.repo = repo  # {"key","owner","name","ref","sha","url","source"}
        self.nodes: dict[str, Node] = {}
        self._edges: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.out_adj: dict[str, list[tuple[str, str]]] = defaultdict(list)
        self.in_adj: dict[str, list[tuple[str, str]]] = defaultdict(list)
        self.stats: dict[str, Any] = {}
        self.warnings: list[str] = []

    # ── building ──
    def add_node(self, node: Node) -> Node:
        existing = self.nodes.get(node.id)
        if existing:
            existing.props.update({k: v for k, v in node.props.items() if v not in (None, "", [])})
            return existing
        self.nodes[node.id] = node
        return node

    def add_edge(self, source: str, target: str, rel: str, **props: Any) -> bool:
        if source == target or source not in self.nodes or target not in self.nodes:
            return False
        key = (source, target, rel)
        if key in self._edges:
            if "count" in self._edges[key]:
                self._edges[key]["count"] += 1
            return False
        self._edges[key] = dict(props)
        self.out_adj[source].append((target, rel))
        self.in_adj[target].append((source, rel))
        return True

    @property
    def edges(self) -> Iterable[tuple[str, str, str, dict[str, Any]]]:
        for (s, t, r), p in self._edges.items():
            yield s, t, r, p

    @property
    def edge_count(self) -> int:
        return len(self._edges)

    def parent_of(self, node_id: str) -> str | None:
        for target, rel in self.out_adj.get(node_id, ()):
            if rel == BELONGS_TO:
                return target
        return None

    # ── serialisation ──
    def to_ui_payload(self, max_nodes: int = 6000, focus: str | None = None) -> dict[str, Any]:
        """Compact JSON for the in-chat view. Drops the least important nodes
        (functions in the deepest/least-connected files first) when over budget."""
        node_ids = list(self.nodes)
        truncated = False
        if len(node_ids) > max_nodes:
            truncated = True
            rank = {FOLDER: 0, **{lbl: 0 for lbl in GENAI_LABELS}, ENDPOINT: 1, SOURCE_FILE: 2, TEST_FILE: 3,
                    TEMPLATE_FILE: 4, DOC_FILE: 5, CLASS: 6, FUNCTION: 7}
            degree = Counter()
            for s, t, r, _ in self.edges:
                if r != BELONGS_TO:
                    degree[s] += 1
                    degree[t] += 1
            node_ids.sort(key=lambda i: (rank.get(self.nodes[i].label, 9), -degree[i]))
            node_ids = node_ids[:max_nodes]
            if focus and focus in self.nodes and focus not in node_ids:
                node_ids[-1] = focus
        keep = set(node_ids)
        edges = [
            {"s": s, "t": t, "r": r, **({"c": p["count"]} if p.get("count", 1) > 1 else {}),
             **({"x": {k: p[k] for k in ("line", "file", "how", "basis", "kind", "mode") if p.get(k) not in (None, "")}}
                if r in GENAI_RELS else {})}
            for s, t, r, p in self.edges
            if s in keep and t in keep
        ]
        return {
            "repo": self.repo,
            "stats": self.stats,
            "nodes": [self.nodes[i].to_ui() for i in node_ids],
            "edges": edges,
            "truncated": truncated,
            "totalNodes": len(self.nodes),
            "totalEdges": self.edge_count,
            "focus": focus,
        }

    def to_json(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "stats": self.stats,
            "warnings": self.warnings,
            "nodes": [
                {"id": n.id, "label": n.label, "name": n.name, "path": n.path, "props": n.props}
                for n in self.nodes.values()
            ],
            "edges": [{"source": s, "target": t, "type": r, **p} for s, t, r, p in self.edges],
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "CodeGraph":
        g = cls(data["repo"])
        g.stats = data.get("stats", {})
        g.warnings = data.get("warnings", [])
        for n in data["nodes"]:
            g.add_node(Node(n["id"], n["label"], n["name"], n.get("path", ""), n.get("props") or {}))
        for e in data["edges"]:
            extra = {k: v for k, v in e.items() if k not in ("source", "target", "type")}
            g.add_edge(e["source"], e["target"], e["type"], **extra)
        return g

    # ── querying ──
    def resolve(self, ref: str) -> Node | None:
        """Find a node by exact id, then exact name/path, then case-insensitive suffix."""
        if ref in self.nodes:
            return self.nodes[ref]
        ref_l = ref.lower().strip()
        exact = [n for n in self.nodes.values() if n.name.lower() == ref_l or n.path.lower() == ref_l]
        if exact:
            return sorted(exact, key=_label_priority)[0]
        qual = [n for n in self.nodes.values()
                if str(n.props.get("qualname") or n.props.get("handler") or "").lower() == ref_l]
        if qual:
            return qual[0]
        suffix = [n for n in self.nodes.values() if n.path.lower().endswith(ref_l) or n.id.lower().endswith(ref_l)]
        if suffix:
            return sorted(suffix, key=lambda n: (len(n.path), _label_priority(n)))[0]
        return None

    def search(self, query: str, label: str | None = None, limit: int = 25) -> list[Node]:
        q = query.lower().strip()
        scored: list[tuple[int, Node]] = []
        for n in self.nodes.values():
            if label and n.label.lower() != label.lower():
                continue
            name = n.name.lower()
            alt = str(n.props.get("qualname") or n.props.get("handler") or "").lower()
            hay = f"{name} {alt} {n.path.lower()} {str(n.props.get('summary', '')).lower()}"
            if name == q or alt == q:
                score = 0
            elif name.startswith(q) or alt.rpartition(".")[2].startswith(q):
                score = 1
            elif q in name or q in alt:
                score = 2
            elif q in n.path.lower():
                score = 3
            elif q in hay:
                score = 4
            else:
                continue
            scored.append((score, n))
        scored.sort(key=lambda x: (x[0], _label_priority(x[1]), len(x[1].name)))
        return [n for _, n in scored[:limit]]

    def neighborhood(
        self,
        center: str,
        depth: int = 1,
        relations: set[str] | None = None,
        direction: str = "both",
        max_nodes: int = 400,
    ) -> "CodeGraph":
        sub = CodeGraph(self.repo)
        sub.stats = dict(self.stats)
        seen = {center}
        frontier = deque([(center, 0)])
        while frontier and len(seen) < max_nodes:
            nid, d = frontier.popleft()
            if d >= depth:
                continue
            nbrs: list[tuple[str, str]] = []
            if direction in ("both", "out"):
                nbrs += self.out_adj.get(nid, [])
            if direction in ("both", "in"):
                nbrs += self.in_adj.get(nid, [])
            for other, rel in nbrs:
                if relations and rel not in relations:
                    continue
                if other not in seen:
                    seen.add(other)
                    frontier.append((other, d + 1))
                    if len(seen) >= max_nodes:
                        break
        # keep the containment chain of the centre so it isn't floating
        cur = center
        for _ in range(12):
            parent = self.parent_of(cur)
            if not parent:
                break
            seen.add(parent)
            cur = parent
        for nid in seen:
            n = self.nodes[nid]
            sub.add_node(Node(n.id, n.label, n.name, n.path, dict(n.props)))
        for s, t, r, p in self.edges:
            if s in seen and t in seen:
                sub.add_edge(s, t, r, **p)
        return sub

    def describe_node(self, node: Node, max_per_rel: int = 15) -> str:
        lines = [f"{node.label} `{node.name}`  (id: {node.id})"]
        loc = node.path
        if node.props.get("line"):
            loc += f":{node.props['line']}"
            if node.props.get("end_line"):
                loc += f"-{node.props['end_line']}"
        if loc:
            lines.append(f"  location: {loc}")
        for key in ("signature", "http_method", "route", "framework", "category", "ai_tool", "tool_name", "runs",
                    "flow", "entry", "test_framework", "language", "loc", "summary", "sdks", "tools_unresolved",
                    "unresolved", "required", "browser_exposed", "warning"):
            val = node.props.get(key)
            if val not in (None, "", [], False):
                lines.append(f"  {key}: {', '.join(map(str, val)) if isinstance(val, list) else val}")
        if node.label == AGENT:  # the flow, step by step, with the code each step runs
            steps = sorted((self.nodes[x] for x, r in self.in_adj.get(node.id, []) if r == BELONGS_TO
                            and self.nodes[x].label == AGENT_STEP), key=lambda n: n.props.get("order", 0))
            for st in steps:
                runs = [self._short(t) + self._where(st.id, t, r) for t, r in self.out_adj.get(st.id, [])
                        if r in (RUNS, USES_TOOL, ROUTES)]
                nxt = [self.nodes[t].name + ("?" if self._edges[(st.id, t, r)].get("kind") != "edge" else "")
                       for t, r in self.out_adj.get(st.id, []) if r == NEXT]
                if st.props.get("can_end"):
                    nxt.append("END")
                lines.append(f"  step {st.props.get('order')}. {st.name}" + (" (entry)" if st.props.get("entry") else "")
                             + (f" runs {'; '.join(runs)}" if runs else (f" runs {st.props['runs']} (not in the graph)" if st.props.get("runs") else ""))
                             + (f" -> {', '.join(nxt)}" if nxt else ""))
        groups: dict[str, list[str]] = defaultdict(list)
        for t, r in self.out_adj.get(node.id, []):
            groups[f"-[{r}]->"].append(self._short(t) + self._where(node.id, t, r))
        for s, r in self.in_adj.get(node.id, []):
            groups[f"<-[{r}]-"].append(self._short(s) + self._where(s, node.id, r))
        for key in sorted(groups):
            vals = groups[key]
            more = f" … +{len(vals) - max_per_rel} more" if len(vals) > max_per_rel else ""
            lines.append(f"  {key} ({len(vals)}): " + ", ".join(vals[:max_per_rel]) + more)
        return "\n".join(lines)

    def _short(self, nid: str) -> str:
        n = self.nodes[nid]
        if n.label in (FUNCTION, CLASS):
            return f"{n.props.get('qualname', n.name)} ({n.path})"
        if n.label == ENDPOINT or n.label in GENAI_LABELS:
            return n.name
        return n.path or n.name

    def _where(self, s: str, t: str, rel: str) -> str:
        """Provenance for GenAI links: the line the link was found on and what it's based on."""
        if rel not in GENAI_RELS:
            return ""
        p = self._edges.get((s, t, rel), {})
        bits = []
        if p.get("line"):
            bits.append(f"{p.get('file') or ''}:{p['line']}".lstrip(":") if p.get("file") else f"line {p['line']}")
        if p.get("how"):
            bits.append(str(p["how"]))
        if p.get("kind") and rel == NEXT:
            bits.append(str(p["kind"]))
        return f" [{'; '.join(bits)}]" if bits else ""


def _label_priority(n: Node) -> int:
    order = {FUNCTION: 0, CLASS: 1, ENDPOINT: 2, SOURCE_FILE: 3, TEST_FILE: 4, FOLDER: 5}
    return order.get(n.label, 6)
