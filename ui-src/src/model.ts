import type { SimulationNodeDatum } from "d3-force";

export type Kind =
  | "Folder" | "DataFile" | "TestingFile" | "DocumentationFile" | "TemplateMarkupFile"
  | "Class" | "Function" | "ApiEndpoint";
export type Shape = "folder" | "file" | "diamond" | "dot" | "hex";

export const KINDS: { key: Kind; name: string; shape: Shape; color: string }[] = [
  { key: "Folder", name: "Folders", shape: "folder", color: "--k-folder" },
  { key: "DataFile", name: "Source files", shape: "file", color: "--k-source" },
  { key: "TestingFile", name: "Tests", shape: "file", color: "--k-test" },
  { key: "DocumentationFile", name: "Docs", shape: "file", color: "--k-doc" },
  { key: "TemplateMarkupFile", name: "Templates", shape: "file", color: "--k-template" },
  { key: "Class", name: "Classes", shape: "diamond", color: "--k-class" },
  { key: "Function", name: "Functions", shape: "dot", color: "--k-function" },
  { key: "ApiEndpoint", name: "Endpoints", shape: "hex", color: "--k-endpoint" },
];
export const KIND = Object.fromEntries(KINDS.map((k) => [k.key, k])) as Record<Kind, (typeof KINDS)[number]>;
export const SINGULAR: Record<Kind, string> = {
  Folder: "Folder", DataFile: "Source file", TestingFile: "Test file", DocumentationFile: "Doc",
  TemplateMarkupFile: "Template", Class: "Class", Function: "Function", ApiEndpoint: "API endpoint",
};
const FILE_KINDS = new Set<Kind>(["DataFile", "TestingFile", "DocumentationFile", "TemplateMarkupFile"]);
export const isFile = (k: Kind) => FILE_KINDS.has(k);

const HTTP = ["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS", "ANY", "WS"];
export const LINK_GROUPS = [
  { key: "contains", name: "Contains", color: "--e-contains" },
  { key: "calls", name: "Calls", color: "--e-calls" },
  { key: "imports", name: "Imports", color: "--e-imports" },
  { key: "http", name: "HTTP requests", color: "--e-http" },
  { key: "tests", name: "Tests", color: "--e-tests" },
  { key: "extends", name: "Extends", color: "--e-extends" },
] as const;
export type Group = (typeof LINK_GROUPS)[number]["key"];

export function groupOf(rel: string): Group {
  if (rel === "BELONGS_TO") return "contains";
  if (rel === "CALLS") return "calls";
  if (rel === "IMPORTS") return "imports";
  if (rel === "TEST") return "tests";
  if (rel === "EXTENDS") return "extends";
  return HTTP.includes(rel) ? "http" : "calls";
}

// how a relationship reads from each end, for the detail panel
export function relPhrase(rel: string, dir: "out" | "in"): string {
  const g = groupOf(rel);
  const table: Record<Group, [string, string]> = {
    contains: ["Inside", "Contains"],
    calls: ["Calls", "Called by"],
    imports: ["Imports", "Imported by"],
    http: ["Requests", "Requested by"],
    tests: ["Tests", "Tested by"],
    extends: ["Extends", "Extended by"],
  };
  return table[g][dir === "out" ? 0 : 1];
}

export interface RawNode { id: string; l: Kind; n: string; p?: string; x?: Record<string, any> }
export interface RawEdge { s: string; t: string; r: string; c?: number }
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
export interface GEdge { s: GNode; t: GNode; rel: string; group: Group; count: number }
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
      const ge: GEdge = { s, t, rel: e.r, group: groupOf(e.r), count: e.c ?? 1 };
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
      n.region = n.kind === "Folder" && n.depth === 1 ? n : top && top.kind === "Folder" ? top : null;
      const second = chain.length >= 2 ? chain[chain.length - 3] ?? null : null;
      n.subregion = n.kind === "Folder" && n.depth === 2 ? n : second && second.kind === "Folder" ? second : null;
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

function radius(n: GNode): number {
  switch (n.kind) {
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
}

export function computeVisible(m: Model, v: VisState): { nodes: GNode[]; links: VLink[] } {
  const memo = new Map<GNode, boolean>();
  const visible = (n: GNode): boolean => {
    const hit = memo.get(n);
    if (hit !== undefined) return hit;
    let ok = !v.focus || v.focus.has(n.id);
    if (ok) for (let a = n.parent; a; a = a.parent) if (v.collapsed.has(a.id)) { ok = false; break; }
    if (ok) {
      if (n.kind === "Function" || n.kind === "Class") ok = v.kinds.has(n.kind) || (!!n.fileOf && v.expanded.has(n.fileOf.id));
      else ok = v.kinds.has(n.kind);
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
