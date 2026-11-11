import type { SimulationNodeDatum } from "d3-force";

export type Kind =
  | "Folder" | "DataFile" | "TestingFile" | "DocumentationFile" | "TemplateMarkupFile"
  | "Class" | "Function" | "ApiEndpoint"
  | "GenAI" | "Agent" | "AgentStep" | "McpServer" | "ExternalService" | "EnvGroup" | "EnvVar";
export type Shape = "folder" | "file" | "diamond" | "dot" | "hex" | "spark" | "agent" | "step" | "server" | "cloud" | "db" | "tag";

export const KINDS: { key: Kind; name: string; shape: Shape; color: string; chip?: false }[] = [
  { key: "Folder", name: "Folders", shape: "folder", color: "--k-folder" },
  { key: "DataFile", name: "Source files", shape: "file", color: "--k-source" },
  { key: "TestingFile", name: "Tests", shape: "file", color: "--k-test" },
  { key: "DocumentationFile", name: "Docs", shape: "file", color: "--k-doc" },
  { key: "TemplateMarkupFile", name: "Templates", shape: "file", color: "--k-template" },
  { key: "Class", name: "Classes", shape: "diamond", color: "--k-class" },
  { key: "Function", name: "Functions", shape: "dot", color: "--k-function" },
  { key: "ApiEndpoint", name: "Endpoints", shape: "hex", color: "--k-endpoint" },
  { key: "GenAI", name: "GenAI & services", shape: "spark", color: "--k-ai", chip: false },
  { key: "Agent", name: "AI agents", shape: "agent", color: "--k-agent" },
  { key: "AgentStep", name: "Agent steps", shape: "step", color: "--k-step" },
  { key: "McpServer", name: "MCP servers", shape: "server", color: "--k-mcp" },
  { key: "ExternalService", name: "Services", shape: "cloud", color: "--k-service" },
  { key: "EnvGroup", name: "Environment", shape: "tag", color: "--k-env", chip: false },
  { key: "EnvVar", name: "Env vars", shape: "tag", color: "--k-env" },
];
export const KIND = Object.fromEntries(KINDS.map((k) => [k.key, k])) as Record<Kind, (typeof KINDS)[number]>;
export const SINGULAR: Record<Kind, string> = {
  Folder: "Folder", DataFile: "Source file", TestingFile: "Test file", DocumentationFile: "Doc",
  TemplateMarkupFile: "Template", Class: "Class", Function: "Function", ApiEndpoint: "API endpoint",
  GenAI: "GenAI layer", Agent: "AI agent", AgentStep: "Agent step", McpServer: "MCP server",
  ExternalService: "External service", EnvGroup: "Environment variables", EnvVar: "Env var",
};
const FILE_KINDS = new Set<Kind>(["DataFile", "TestingFile", "DocumentationFile", "TemplateMarkupFile"]);
export const isFile = (k: Kind) => FILE_KINDS.has(k);
export const GENAI_KINDS = new Set<Kind>(["GenAI", "Agent", "AgentStep", "McpServer", "ExternalService", "EnvGroup", "EnvVar"]);
export const isGenAI = (k: Kind) => GENAI_KINDS.has(k);
/** Kinds shown only inside their opened parent (an agent's steps, the env vars) unless turned on in the legend. */
export const NESTED_KINDS = new Set<Kind>(["AgentStep", "EnvVar"]);
/** Double-click breaks these down into the code and parts behind them. */
export const OPENABLE = new Set<Kind>(["Agent", "AgentStep", "McpServer", "ExternalService", "EnvGroup", "EnvVar"]);
const DATASTORES = new Set(["Vector DB", "Graph DB", "Database", "Cache"]);

export function shapeOf(n: { kind: Kind; props: Record<string, any> }): Shape {
  if (n.kind === "ExternalService" && DATASTORES.has(n.props.category)) return "db";
  return KIND[n.kind].shape;
}

const HTTP = ["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS", "ANY", "WS"];
export const LINK_GROUPS = [
  { key: "contains", name: "Contains", color: "--e-contains" },
  { key: "calls", name: "Calls", color: "--e-calls" },
  { key: "imports", name: "Imports", color: "--e-imports" },
  { key: "http", name: "HTTP requests", color: "--e-http" },
  { key: "tests", name: "Tests", color: "--e-tests" },
  { key: "extends", name: "Extends", color: "--e-extends" },
  { key: "flow", name: "Agent flow", color: "--e-flow" },
  { key: "runs", name: "Runs & tools", color: "--e-runs" },
  { key: "uses", name: "Uses service", color: "--e-uses" },
  { key: "env", name: "Reads env", color: "--e-env" },
] as const;
export type Group = (typeof LINK_GROUPS)[number]["key"];

export function groupOf(rel: string): Group {
  if (rel === "BELONGS_TO") return "contains";
  if (rel === "CALLS") return "calls";
  if (rel === "IMPORTS") return "imports";
  if (rel === "TEST") return "tests";
  if (rel === "EXTENDS") return "extends";
  if (rel === "NEXT") return "flow";
  if (rel === "RUNS" || rel === "ROUTES" || rel === "USES_TOOL" || rel === "EXPOSES") return "runs";
  if (rel === "USES") return "uses";
  if (rel === "READS_ENV") return "env";
  return HTTP.includes(rel) ? "http" : "calls";
}

const REL_PHRASE: Record<string, [string, string]> = {
  NEXT: ["Goes to", "Comes after"],
  RUNS: ["Runs", "Run by"],
  ROUTES: ["Decided by", "Decides for"],
  USES_TOOL: ["Uses tools", "Tool of"],
  EXPOSES: ["Exposes", "Exposed by"],
  USES: ["Uses", "Used by"],
  READS_ENV: ["Reads", "Read by"],
};

// how a relationship reads from each end, for the detail panel
export function relPhrase(rel: string, dir: "out" | "in"): string {
  if (REL_PHRASE[rel]) return REL_PHRASE[rel][dir === "out" ? 0 : 1];
  const g = groupOf(rel);
  const table: Record<Group, [string, string]> = {
    contains: ["Inside", "Contains"],
    calls: ["Calls", "Called by"],
    imports: ["Imports", "Imported by"],
    http: ["Requests", "Requested by"],
    tests: ["Tests", "Tested by"],
    extends: ["Extends", "Extended by"],
    flow: ["Goes to", "Comes after"],
    runs: ["Runs", "Run by"],
    uses: ["Uses", "Used by"],
    env: ["Reads", "Read by"],
  };
  return table[g][dir === "out" ? 0 : 1];
}

export interface RawNode { id: string; l: Kind; n: string; p?: string; x?: Record<string, any> }
export interface RawEdge { s: string; t: string; r: string; c?: number; x?: Record<string, any> }
export interface Payload {
  repo: Record<string, any>;
  stats: Record<string, any>;
  nodes: RawNode[];
  edges: RawEdge[];
  truncated?: boolean;
  totalNodes?: number;
  totalEdges?: number;
  focus?: string | null;
  /** Agent mode: the payload is the whole graph; filter the view to this many hops around `focus`. */
  focusDepth?: number;
  graphId: string;
  mode?: "repo" | "explore";
  error?: string;
}

export interface GNode extends SimulationNodeDatum {
  id: string;
  kind: Kind;
  name: string;
  path: string;
  props: Record<string, any>;
  parent: GNode | null;
  children: GNode[];
  depth: number;
  region: GNode | null; // top-level folder this node lives in
  subregion: GNode | null; // second-level folder
  fileOf: GNode | null; // owning file for classes/functions/endpoints
  deg: number;
  r: number;
  inE: GEdge[];
  outE: GEdge[];
}
export interface GEdge { s: GNode; t: GNode; rel: string; group: Group; count: number; props?: Record<string, any> }
export interface VLink { source: GNode; target: GNode; group: Group; count: number; lifted: boolean }

export class Model {
  nodes: GNode[] = [];
  byId = new Map<string, GNode>();
  edges: GEdge[] = [];
  root: GNode | null = null;
  kindCounts = new Map<Kind, number>();
  groupCounts = new Map<Group, number>();

  constructor(public payload: Payload) {
    for (const r of payload.nodes) {
      const n: GNode = {
        id: r.id, kind: r.l, name: r.n, path: r.p ?? "", props: r.x ?? {}, parent: null, children: [],
        depth: 0, region: null, subregion: null, fileOf: null, deg: 0, r: 4, inE: [], outE: [],
      };
      this.nodes.push(n);
      this.byId.set(n.id, n);
      this.kindCounts.set(n.kind, (this.kindCounts.get(n.kind) ?? 0) + 1);
    }
    for (const e of payload.edges) {
      const s = this.byId.get(e.s), t = this.byId.get(e.t);
      if (!s || !t) continue;
      const ge: GEdge = { s, t, rel: e.r, group: groupOf(e.r), count: e.c ?? 1, props: e.x };
      this.edges.push(ge);
      s.outE.push(ge);
      t.inE.push(ge);
      this.groupCounts.set(ge.group, (this.groupCounts.get(ge.group) ?? 0) + 1);
      if (ge.group === "contains") {
        if (!s.parent) {
          s.parent = t;
          t.children.push(s);
        }
      } else {
        s.deg += 1;
        t.deg += 1;
      }
    }
    this.root = this.nodes.find((n) => n.props.is_root) ?? this.nodes.find((n) => !n.parent && n.kind === "Folder") ?? null;
    for (const n of this.nodes) {
      const chain: GNode[] = [];
      for (let a = n.parent; a && chain.length < 64; a = a.parent) chain.push(a);
      n.depth = chain.length;
      const top = chain.length >= 1 ? chain[chain.length - 2] ?? null : null; // ancestor just under root
      n.region = isRegion(n.kind) && n.depth === 1 ? n : top && isRegion(top.kind) ? top : null;
      const second = chain.length >= 2 ? chain[chain.length - 3] ?? null : null;
      n.subregion = isSubregion(n.kind) && n.depth === 2 ? n : second && isSubregion(second.kind) ? second : null;
      n.fileOf = isFile(n.kind) ? null : chain.find((a) => isFile(a.kind)) ?? null;
      n.r = radius(n);
    }
  }

  get repoLabel(): string {
    const r = this.payload.repo ?? {};
    return (r.display ?? r.name ?? "repository") + (r.subpath ? `/${r.subpath}` : "");
  }

  fileUrl(n: GNode): string | null {
    const base = this.payload.repo?.blob_base;
    if (!base || !n.path) return null;
    const line = n.props.line ? `#L${n.props.line}${n.props.end_line && n.props.end_line !== n.props.line ? `-L${n.props.end_line}` : ""}` : "";
    return base + n.path.split("/").map(encodeURIComponent).join("/") + line;
  }
}

const isRegion = (k: Kind) => k === "Folder" || k === "GenAI";
const isSubregion = (k: Kind) => k === "Folder" || k === "Agent" || k === "EnvGroup";

function radius(n: GNode): number {
  switch (n.kind) {
    case "GenAI": return 10;
    case "Agent": return 8.5;
    case "AgentStep": return 5.2;
    case "McpServer": return 7;
    case "ExternalService": return Math.min(9.5, 6 + Math.sqrt(n.deg) * 0.35);
    case "EnvGroup": return 7;
    case "EnvVar": return 4.2;
    case "Folder": {
      const files = n.props.file_count ?? n.children.length;
      return n.props.is_root ? 9 : Math.min(15, 5 + Math.sqrt(files) * 1.3);
    }
    case "ApiEndpoint":
      return 5.5;
    case "Class":
      return Math.min(8, 4.5 + Math.sqrt(n.children.length) * 0.6);
    case "Function":
      return Math.min(7, 3.3 + Math.sqrt(n.deg) * 0.7);
    default:
      return Math.min(9.5, 5 + Math.sqrt(n.children.length) * 0.6);
  }
}

export interface VisState {
  kinds: Set<Kind>;
  groups: Set<Group>;
  expanded: Set<string>; // files whose classes/functions are shown even when those kinds are off
  collapsed: Set<string>; // folders whose contents are folded into them
  focus: Set<string> | null; // restrict to these ids
  opened: Set<string>; // GenAI nodes broken down into the code and parts behind them
}

/** What double-clicking a GenAI node reveals: an agent's steps and the functions, routers and tools
 *  they run; the functions an MCP server exposes; the code that uses a service or reads an env var. */
export function breakdown(n: GNode): GNode[] {
  const out: GNode[] = [];
  const along = (x: GNode, rels: string[]) => { for (const e of x.outE) if (rels.includes(e.rel)) out.push(e.t); };
  switch (n.kind) {
    case "Agent":
      along(n, ["USES_TOOL", "RUNS"]);
      for (const st of n.children) {
        out.push(st);
        along(st, ["RUNS", "ROUTES", "USES_TOOL"]);
      }
      for (const f of [...out]) if (f.kind === "Function" || f.kind === "ApiEndpoint") along(f, ["USES_TOOL"]);
      break;
    case "AgentStep":
      along(n, ["RUNS", "ROUTES", "USES_TOOL"]);
      break;
    case "McpServer":
      along(n, ["EXPOSES"]);
      break;
    case "ExternalService":
      for (const e of n.inE) if (e.rel === "USES") out.push(e.s);
      break;
    case "EnvGroup":
      out.push(...n.children);
      break;
    case "EnvVar":
      for (const e of n.inE) if (e.rel === "READS_ENV") out.push(e.s);
      break;
  }
  return [...new Set(out)];
}

export function computeVisible(m: Model, v: VisState): { nodes: GNode[]; links: VLink[] } {
  const memo = new Map<GNode, boolean>();
  const revealed = new Set<GNode>();
  for (const id of v.opened) {
    const o = m.byId.get(id);
    if (o) for (const r of breakdown(o)) revealed.add(r);
  }
  const visible = (n: GNode): boolean => {
    const hit = memo.get(n);
    if (hit !== undefined) return hit;
    let ok = revealed.has(n) && (n.parent === null || !isGenAI(n.kind) || visible(n.parent) || v.opened.has(n.parent.id));
    if (!ok) {
      ok = !v.focus || v.focus.has(n.id);
      if (ok) for (let a = n.parent; a; a = a.parent) if (v.collapsed.has(a.id)) { ok = false; break; }
      if (ok) {
        if (n.kind === "Function" || n.kind === "Class") ok = v.kinds.has(n.kind) || (!!n.fileOf && v.expanded.has(n.fileOf.id));
        else if (NESTED_KINDS.has(n.kind)) ok = v.kinds.has(n.kind) || (!!n.parent && v.opened.has(n.parent.id));
        else ok = v.kinds.has(n.kind);
      }
    }
    memo.set(n, ok);
    return ok;
  };
  const rep = (n: GNode): GNode | null => {
    for (let a: GNode | null = n; a; a = a.parent) if (visible(a)) return a;
    return null;
  };
  // Links are lifted onto visible containers only through folded folders and folded-in code
  // (functions/classes inside a file). If the user hid a whole kind (e.g. Tests), its links go too.
  const kindHidden = (n: GNode): boolean => {
    const f = isFile(n.kind) ? n : n.fileOf;
    if (f && !v.kinds.has(f.kind)) return true;
    if (isGenAI(n.kind) && !NESTED_KINDS.has(n.kind) && n.kind !== "GenAI" && !v.kinds.has(n.kind)) return true;
    return n.kind === "ApiEndpoint" && !v.kinds.has("ApiEndpoint");
  };
  const nodes = m.nodes.filter(visible);
  const agg = new Map<string, VLink>();
  for (const e of m.edges) {
    if (!v.groups.has(e.group)) continue;
    if (e.group !== "contains" && (kindHidden(e.s) || kindHidden(e.t))) continue;
    let s: GNode | null, t: GNode | null;
    if (e.group === "contains") {
      if (!visible(e.s)) continue;
      s = e.s;
      t = rep(e.t);
    } else {
      s = rep(e.s);
      t = rep(e.t);
    }
    if (!s || !t || s === t) continue;
    if (e.group !== "contains" && (s === t.parent || t === s.parent) && (s !== e.s || t !== e.t)) continue; // lifted onto own container
    const key = `${s.id}\u0000${t.id}\u0000${e.group}`;
    const cur = agg.get(key);
    if (cur) cur.count += e.count;
    else agg.set(key, { source: s, target: t, group: e.group, count: e.count, lifted: s !== e.s || t !== e.t });
  }
  return { nodes, links: [...agg.values()] };
}

export function neighbourhood(m: Model, center: GNode, depth = 1): Set<string> {
  const keep = new Set<string>([center.id]);
  let frontier = [center];
  for (let d = 0; d < depth; d++) {
    const next: GNode[] = [];
    for (const n of frontier) {
      for (const e of [...n.outE, ...n.inE]) {
        const o = e.s === n ? e.t : e.s;
        if (!keep.has(o.id)) { keep.add(o.id); next.push(o); }
      }
    }
    frontier = next;
  }
  for (const id of [...keep]) for (let a = m.byId.get(id)?.parent; a; a = a.parent) keep.add(a.id);
  return keep;
}
