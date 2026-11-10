// The layered overview: the repo as a few calm columns (Frontend → API → Backend → AI & services),
// with bundled arrows between them. Every row opens the matching part of the graph.
import { isFile, type GNode, type Group, type Kind, type Model } from "./model";

export type ColKey = "frontend" | "api" | "backend" | "ai";

export interface OvItem {
  id: string;
  col: ColKey;
  label: string;
  prefix?: string; // muted path before the label ("frontend/")
  meta?: string;
  title?: string; // full text for the tooltip
  kind: Kind;
  node?: GNode; // the one node this row stands for (endpoint, agent, service…)
  members: GNode[]; // files of a folder group
  warn?: boolean;
  overflow?: boolean;
}
export interface OvSection { title?: string; items: OvItem[] }
export interface OvColumn { key: ColKey; title: string; sub: string; sections: OvSection[] }
export interface OvLink { from: OvItem; to: OvItem; group: Group; count: number }
export interface Overview {
  columns: OvColumn[];
  links: OvLink[];
  itemCount: number;
  /** The row a node is counted under (its folder group, its endpoint, its agent…). */
  itemOf(n: GNode): OvItem | null;
  /** The folder row a path falls in: for things that no longer exist in this graph. */
  itemOfPath(path: string): OvItem | null;
}
export interface OverviewHandle {
  destroy(): void;
  /** Change counts per row ([added, changed, removed]) and the banner saying what they're from. */
  mark(counts: Map<OvItem, [number, number, number]> | null, banner?: { label: string; detail: string; onClear(): void }): void;
}

const CODE = new Set<Kind>(["DataFile", "TemplateMarkupFile"]);
const UI_EXT = /\.(tsx|jsx|vue|svelte|html?|astro)$/i;
const JS_EXT = /\.(m?[jt]sx?|cjs|mts|cts|vue|svelte|astro)$/i;
const FE_DIR = /^(frontend|front-end|web|webapp|client|clients|ui|www|site|dashboard|portal|mobile)$/i;
const DRAWN = new Set<Group>(["http", "calls", "uses", "runs"]);
const MAX_ROWS = 7;
const fmt = new Intl.NumberFormat();
const plural = (n: number, w: string) => `${fmt.format(n)} ${w}${n === 1 ? "" : "s"}`;

/** Folder prefix of a file at depth d ("frontend/components" for d = 2); shallower files keep their own folder. */
function prefix(path: string, d: number): string {
  const dirs = path.split("/").slice(0, -1);
  return dirs.slice(0, d).join("/");
}

export function buildOverview(m: Model): Overview {
  const files = m.nodes.filter((n) => CODE.has(n.kind));
  const tests = m.nodes.filter((n) => n.kind === "TestingFile");

  // ── folder groups: by top-level folder, splitting any group that holds most of the code ──
  let groups = new Map<string, GNode[]>();
  for (const f of files) {
    const k = prefix(f.path, 1);
    (groups.get(k) ?? groups.set(k, []).get(k)!).push(f);
  }
  for (let pass = 0; pass < 3; pass++) {
    let changed = false;
    const next = new Map<string, GNode[]>();
    for (const [key, fs] of groups) {
      const depth = key ? key.split("/").length : 0;
      if (key && fs.length > Math.max(8, files.length * 0.4)) {
        const kids = new Map<string, GNode[]>();
        for (const f of fs) {
          const k = prefix(f.path, depth + 1);
          (kids.get(k) ?? kids.set(k, []).get(k)!).push(f);
        }
        if (kids.size > 1) {
          for (const [k, v] of kids) next.set(k, v);
          changed = true;
          continue;
        }
      }
      next.set(key, fs);
    }
    groups = next;
    if (!changed) break;
  }

  // ── which groups are frontend ──
  const epFile = new Set<GNode>();
  const endpoints = m.nodes.filter((n) => n.kind === "ApiEndpoint");
  for (const e of endpoints) {
    if (e.props.defined_in_repo === false) continue;
    if (e.fileOf) epFile.add(e.fileOf);
  }
  const httpFrom = new Set<GNode>();
  for (const e of m.edges) if (e.group === "http") { const f = isFile(e.s.kind) ? e.s : e.s.fileOf; if (f) httpFrom.add(f); }
  const isFrontend = (key: string, fs: GNode[]) => {
    if (fs.some((f) => epFile.has(f))) return false;
    const js = fs.filter((f) => JS_EXT.test(f.path)).length;
    if (js < fs.length * 0.6) return false;
    const ui = fs.some((f) => UI_EXT.test(f.path));
    const top = key.split("/")[0];
    return ui || fs.some((f) => httpFrom.has(f)) || FE_DIR.test(top);
  };

  const fileItem = new Map<GNode, OvItem>();
  const common: Partial<Record<ColKey, string>> = {};
  const makeGroups = (col: ColKey, entries: [string, GNode[]][]): OvItem[] => {
    entries.sort((a, b) => b[1].length - a[1].length);
    // a folder every row starts with ("frontend/") is said once, in the column heading
    const keys = entries.map(([k]) => k);
    let base = keys.length > 1 && keys.every((k) => k) ? keys[0].split("/")[0] : "";
    if (base && !keys.every((k) => k === base || k.startsWith(base + "/"))) base = "";
    common[col] = base;
    const items: OvItem[] = [];
    entries.forEach(([key, fs], i) => {
      const rel = base ? key.slice(base.length + 1) : key;
      const parts = rel.split("/");
      const direct = [...groups.keys()].some((k) => k !== key && k.startsWith(key + "/"));
      let item: OvItem;
      if (i >= MAX_ROWS && entries.length > MAX_ROWS + 1) {
        item = items[MAX_ROWS] ?? (items[MAX_ROWS] = {
          id: `${col}:more`, col, label: "", meta: "", kind: "Folder", members: [], overflow: true,
        });
        item.members.push(...fs);
        item.label = `+${entries.length - MAX_ROWS} more folders`;
        item.meta = plural(item.members.length, "file");
      } else {
        item = {
          id: `${col}:${key || "(root)"}`, col, kind: "Folder", members: fs,
          label: rel ? parts[parts.length - 1] + "/" : "top-level files",
          // a long path before the name is shortened so the name itself stays readable
          prefix: rel && parts.length > 1 ? (parts.length > 2 || parts[0].length > 12 ? "…/" : parts[0] + "/") : undefined,
          meta: plural(fs.length, "file") + (direct ? " here" : ""),
          title: key ? `${key}/` : "Files at the top of the repository",
        };
        items.push(item);
      }
      for (const f of fs) fileItem.set(f, item);
    });
    return items;
  };
  const fe: [string, GNode[]][] = [];
  const be: [string, GNode[]][] = [];
  for (const [k, fs] of groups) (isFrontend(k, fs) ? fe : be).push([k, fs]);
  const feItems = makeGroups("frontend", fe);
  const beItems = makeGroups("backend", be);

  let testsItem: OvItem | null = null;
  if (tests.length) {
    testsItem = { id: "backend:tests", col: "backend", kind: "TestingFile", label: "tests", meta: plural(tests.length, "file"), members: tests };
  }

  // ── API: endpoints defined here, most-requested first ──
  const nodeItem = new Map<GNode, OvItem>();
  const defined = endpoints.filter((e) => e.props.defined_in_repo !== false);
  const requests = (n: GNode) => n.inE.filter((e) => e.group === "http").length;
  defined.sort((a, b) => requests(b) - requests(a) || String(a.props.route).localeCompare(String(b.props.route)));
  const apiFiles = new Set(defined.map((n) => n.path));
  const apiItems = capped("api", defined, (n) => ({
    label: n.name, meta: apiFiles.size > 1 ? n.path.split("/").pop() : undefined,
    title: `${n.name}  (${n.path}:${n.props.line ?? "?"})`,
  }), "endpoints", nodeItem);

  // ── AI & services ──
  const ai: OvSection[] = [];
  const agents = m.nodes.filter((n) => n.kind === "Agent");
  if (agents.length) ai.push({ title: "Agents", items: capped("ai", agents, (n) => ({
    label: n.name, meta: n.props.steps ? plural(n.props.steps, "step") : String(n.props.framework ?? "").split(" ")[0],
    title: `${n.name} — ${n.props.framework ?? ""} (${n.path}:${n.props.line ?? "?"})`,
  }), "agents", nodeItem) });
  const mcps = m.nodes.filter((n) => n.kind === "McpServer");
  if (mcps.length) ai.push({ title: "MCP servers", items: capped("ai", mcps, (n) => ({
    label: n.name, meta: n.props.tools ? plural(n.props.tools, "tool") : "", title: `${n.name} (${n.path})`,
  }), "servers", nodeItem) });
  const uses = (n: GNode) => n.inE.filter((e) => e.rel === "USES").length;
  const services = m.nodes.filter((n) => n.kind === "ExternalService").sort((a, b) => uses(b) - uses(a));
  if (services.length) ai.push({ title: "Services", items: capped("ai", services, (n) => ({
    label: n.name, meta: String(n.props.category ?? ""), title: `${n.name} — ${n.props.category ?? ""}, ${plural(uses(n), "use site")}`,
  }), "services", nodeItem) });
  const envGroup = m.nodes.find((n) => n.kind === "EnvGroup");
  if (envGroup) {
    const items: OvItem[] = [{
      id: "ai:env", col: "ai", kind: "EnvGroup", node: envGroup, members: [], label: plural(envGroup.props.count ?? 0, "env var"),
      meta: envGroup.props.required ? `${envGroup.props.required} required` : "", title: "Environment variables read in the code",
    }];
    for (const v of m.nodes.filter((n) => n.kind === "EnvVar" && n.props.warning)) {
      const it: OvItem = { id: `ai:${v.id}`, col: "ai", kind: "EnvVar", node: v, members: [], label: v.name, meta: "in browser JS", warn: true,
        title: String(v.props.warning) };
      items.push(it);
      nodeItem.set(v, it);
    }
    for (const v of m.nodes) if (v.kind === "EnvVar" && !nodeItem.has(v)) nodeItem.set(v, items[0]);
    nodeItem.set(envGroup, items[0]);
    ai.push({ title: "Environment", items });
  }
  const outside = endpoints.filter((e) => e.props.defined_in_repo === false);
  if (outside.length) ai.push({ title: "Called outside this repo", items: capped("ai", outside, (n) => ({
    label: n.name, title: `${n.name}: requested by the code, but not defined in this repository`,
  }), "endpoints", nodeItem, 3) });

  // ── columns ──
  const columns: OvColumn[] = [];
  const files$ = (items: OvItem[]) => items.reduce((s, i) => s + i.members.length, 0);
  const where = (col: ColKey) => (common[col] ? `${common[col]}/ · ` : "");
  if (feItems.length) columns.push({ key: "frontend", title: "Frontend", sub: where("frontend") + plural(files$(feItems), "file"), sections: [{ items: feItems }] });
  if (apiItems.length) columns.push({ key: "api", title: "API", sub: plural(defined.length, "endpoint") + (apiFiles.size === 1 ? ` · ${[...apiFiles][0].split("/").pop()}` : ""), sections: [{ items: apiItems }] });
  if (beItems.length || testsItem) {
    const sections: OvSection[] = [{ items: beItems }];
    if (testsItem) sections.push({ title: "Tests", items: [testsItem] });
    columns.push({ key: "backend", title: feItems.length || apiItems.length ? "Backend" : "Code", sub: where("backend") + plural(files$(beItems), "file"), sections });
  }
  if (ai.length) {
    const bits = [agents.length && plural(agents.length, "agent"), services.length && plural(services.length, "service"),
      envGroup && plural(envGroup.props.count ?? 0, "env var")].filter(Boolean);
    columns.push({ key: "ai", title: "AI & services", sub: bits.join(" · "), sections: ai });
  }

  // ── bundled links between rows of different columns ──
  const itemOf = (n: GNode): OvItem | null => {
    if (nodeItem.has(n)) return nodeItem.get(n)!;
    if (n.kind === "AgentStep" && n.parent) return itemOf(n.parent);
    const f = isFile(n.kind) ? n : n.fileOf;
    if (!f || f.kind === "TestingFile" || f.kind === "DocumentationFile") return null;
    return fileItem.get(f) ?? null;
  };
  const agg = new Map<string, OvLink>();
  for (const e of m.edges) {
    if (!DRAWN.has(e.group)) continue;
    const a = itemOf(e.s), b = itemOf(e.t);
    if (!a || !b || a === b || a.col === b.col) continue;
    const key = `${a.id}\u0000${b.id}\u0000${e.group}`;
    const cur = agg.get(key);
    if (cur) cur.count += e.count;
    else agg.set(key, { from: a, to: b, group: e.group, count: e.count });
  }
  const itemCount = columns.reduce((s, c) => s + c.sections.reduce((t, x) => t + x.items.length, 0), 0);
  const keys = [...groups.keys()].sort((a, b) => b.length - a.length);
  const itemOfPath = (path: string): OvItem | null => {
    if (/(^|\/)(tests?|__tests__|spec)\//.test(path) && testsItem) return testsItem;
    for (const k of keys) {
      if (k === "" ? !path.includes("/") || keys.length === 1 : path === k || path.startsWith(k + "/")) {
        const first = groups.get(k)?.[0];
        if (first) return fileItem.get(first) ?? null;
      }
    }
    return null;
  };
  return { columns, links: [...agg.values()], itemCount, itemOf, itemOfPath };
}

function capped(col: ColKey, nodes: GNode[], describe: (n: GNode) => Partial<OvItem>, noun: string,
                map: Map<GNode, OvItem>, max = MAX_ROWS): OvItem[] {
  const out: OvItem[] = [];
  nodes.forEach((n, i) => {
    if (i >= max && nodes.length > max + 1) {
      const more = out[max] ?? (out[max] = { id: `${col}:${noun}:more`, col, kind: n.kind, members: [], label: "", overflow: true });
      more.label = `+${nodes.length - max} more ${noun}`;
      map.set(n, more);
      return;
    }
    const it: OvItem = { id: `${col}:${n.id}`, col, kind: n.kind, node: n, members: [], label: n.name, ...describe(n) };
    out.push(it);
    map.set(n, it);
  });
  return out;
}

// ── rendering ────────────────────────────────────────────────────────────────

export interface OverviewCallbacks {
  glyph(kind: Kind, n?: GNode): HTMLElement;
  open(item: OvItem): void;
}

const LINK_WORD: Record<string, [string, string]> = {
  http: ["request", "requests"], calls: ["call", "calls"], uses: ["use", "uses"], runs: ["link", "links"],
};

export function renderOverview(host: HTMLElement, ov: Overview, repoName: string, cb: OverviewCallbacks): OverviewHandle {
  const el = <K extends keyof HTMLElementTagNameMap>(tag: K, cls?: string, text?: string) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  };
  host.replaceChildren();
  const board = el("div", "ov-board");
  const intro = el("p", "ov-intro");
  intro.append("How ", el("strong", "", repoName), " fits together. Click anything to open it as a graph.");
  const grid = el("div", "ov-grid");
  grid.style.setProperty("--cols", String(ov.columns.length));
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.classList.add("ov-links");
  svg.setAttribute("aria-hidden", "true");
  const banner = el("div", "ov-banner");
  banner.hidden = true;
  board.append(banner, intro, grid, svg);

  const rowEl = new Map<OvItem, HTMLElement>();
  const colIndex = new Map<ColKey, number>(ov.columns.map((c, i) => [c.key, i]));
  for (const col of ov.columns) {
    const sec = el("section", `ov-col ov-col-${col.key}`);
    const head = el("header", "ov-col-head");
    head.append(el("h3", "ov-col-title", col.title), el("p", "ov-col-sub", col.sub));
    sec.append(head);
    for (const s of col.sections) {
      const card = el("div", "ov-card");
      if (s.title) card.append(el("p", "ov-sec-title", s.title));
      for (const it of s.items) {
        const b = el("button", `ov-row${it.warn ? " is-warn" : ""}${it.overflow ? " is-more" : ""}`);
        b.type = "button";
        if (!it.overflow) b.append(cb.glyph(it.kind, it.node));
        const name = el("span", "ov-name");
        if (it.prefix) name.append(el("span", "ov-prefix", it.prefix));
        name.append(it.label);
        b.append(name);
        if (it.meta) b.append(el("span", "ov-meta", it.meta));
        b.title = it.title ?? it.label;
        b.onclick = () => cb.open(it);
        b.onmouseenter = b.onfocus = () => hot(it);
        b.onmouseleave = b.onblur = () => hot(null);
        rowEl.set(it, b);
        card.append(b);
      }
      sec.append(card);
    }
    grid.append(sec);
  }
  const key = el("p", "ov-key");
  const groupsShown = new Set(ov.links.map((l) => l.group));
  for (const [g, word] of [["http", "requests"], ["calls", "calls"], ["uses", "uses a service"], ["runs", "runs / exposes"]] as const) {
    if (!groupsShown.has(g)) continue;
    key.append(el("span", `swatch swatch-${g}`), el("span", "ov-key-word", word));
  }
  if (key.childElementCount) board.append(key);
  host.append(board);

  // ── arrows ──
  const NS = "http://www.w3.org/2000/svg";
  const paths: { link: OvLink; path: SVGPathElement; label: SVGTextElement }[] = [];
  let hotItem: OvItem | null = null;
  function hot(it: OvItem | null): void {
    hotItem = it;
    board.classList.toggle("is-hovering", !!it);
    const linked = new Set<OvItem>();
    for (const p of paths) {
      const on = !!it && (p.link.from === it || p.link.to === it);
      p.path.classList.toggle("is-hot", on);
      p.label.classList.toggle("is-hot", on);
      if (on) { linked.add(p.link.from); linked.add(p.link.to); }
    }
    for (const [item, e] of rowEl) {
      e.classList.toggle("is-hot", item === it);
      e.classList.toggle("is-linked", linked.has(item) && item !== it);
    }
  }

  function layout(): void {
    svg.replaceChildren();
    paths.length = 0;
    const stacked = getComputedStyle(grid).getPropertyValue("--stacked").trim() === "1";
    if (stacked || !ov.links.length) return;
    const box = board.getBoundingClientRect();
    svg.setAttribute("width", String(board.scrollWidth));
    svg.setAttribute("height", String(board.scrollHeight));
    const defs = document.createElementNS(NS, "defs");
    for (const g of DRAWN) {
      const mk = document.createElementNS(NS, "marker");
      mk.setAttribute("id", `ov-ah-${g}`);
      mk.setAttribute("viewBox", "0 0 8 8");
      mk.setAttribute("refX", "7");
      mk.setAttribute("refY", "4");
      mk.setAttribute("markerUnits", "userSpaceOnUse");
      mk.setAttribute("markerWidth", "8");
      mk.setAttribute("markerHeight", "8");
      mk.setAttribute("orient", "auto-start-reverse");
      const tip = document.createElementNS(NS, "path");
      tip.setAttribute("d", "M0,0 L8,4 L0,8 z");
      tip.setAttribute("style", `fill: var(--e-${g})`);
      mk.append(tip);
      defs.append(mk);
    }
    svg.append(defs);
    // ports: spread the links that leave/enter the same side of a row so they fan out
    type Side = "L" | "R";
    const ports = new Map<string, { link: OvLink; end: "from" | "to"; otherY: number }[]>();
    const rect = (it: OvItem) => {
      const row = rowEl.get(it)!;
      const r = row.getBoundingClientRect();
      const c = (row.closest(".ov-card") ?? row).getBoundingClientRect();
      return { l: c.left - box.left, r: c.right - box.left, t: r.top - box.top, b: r.bottom - box.top };
    };
    const sideOf = (l: OvLink, end: "from" | "to"): Side => {
      const fwd = colIndex.get(l.from.col)! < colIndex.get(l.to.col)!;
      return end === "from" ? (fwd ? "R" : "L") : (fwd ? "L" : "R");
    };
    for (const l of ov.links) {
      for (const end of ["from", "to"] as const) {
        const it = l[end], other = end === "from" ? l.to : l.from;
        const k = `${it.id}|${sideOf(l, end)}`;
        const o = rect(other);
        (ports.get(k) ?? ports.set(k, []).get(k)!).push({ link: l, end, otherY: (o.t + o.b) / 2 });
      }
    }
    const portY = new Map<string, number>();
    for (const [k, list] of ports) {
      const id = k.slice(0, k.lastIndexOf("|"));
      const it = list[0].link[list[0].end];
      if (it.id !== id) continue;
      const r = rect(it);
      list.sort((a, b) => a.otherY - b.otherY);
      const h = r.b - r.t, n = list.length;
      list.forEach((p, i) => portY.set(`${k}|${p.link.from.id}>${p.link.to.id}>${p.link.group}`, r.t + h * (0.5 + (n > 1 ? (i / (n - 1) - 0.5) * 0.6 : 0))));
    }
    const yOf = (l: OvLink, end: "from" | "to") =>
      portY.get(`${l[end].id}|${sideOf(l, end)}|${l.from.id}>${l.to.id}>${l.group}`)!;
    const max = Math.max(...ov.links.map((l) => l.count));
    for (const l of [...ov.links].sort((a, b) => a.count - b.count)) {
      const a = rect(l.from), b = rect(l.to);
      const fwd = colIndex.get(l.from.col)! < colIndex.get(l.to.col)!;
      const x1 = fwd ? a.r : a.l, x2 = fwd ? b.l - 1 : b.r + 1;
      const y1 = yOf(l, "from"), y2 = yOf(l, "to");
      const dx = Math.max(36, Math.abs(x2 - x1) * 0.45) * (fwd ? 1 : -1);
      const path = document.createElementNS(NS, "path");
      path.setAttribute("d", `M${x1},${y1} C${x1 + dx},${y1} ${x2 - dx},${y2} ${x2},${y2}`);
      const long = Math.abs(colIndex.get(l.from.col)! - colIndex.get(l.to.col)!) > 1;
      path.setAttribute("class", `ov-link ov-link-${l.group}${long ? " is-long" : ""}`);
      path.setAttribute("style", `stroke: var(--e-${l.group}); stroke-width: ${(1.1 + 1.7 * Math.sqrt(l.count / max)).toFixed(2)}px`);
      path.setAttribute("marker-end", `url(#ov-ah-${l.group})`);
      const words = LINK_WORD[l.group] ?? ["link", "links"];
      const label = document.createElementNS(NS, "text");
      // near the arrow's end, where links fan out to different rows, so their labels don't stack
      const t = 0.74, u = 1 - t;
      const cx1 = x1 + dx, cx2 = x2 - dx;
      const mx = u * u * u * x1 + 3 * u * u * t * cx1 + 3 * u * t * t * cx2 + t * t * t * x2;
      const my = u * u * u * y1 + 3 * u * u * t * y1 + 3 * u * t * t * y2 + t * t * t * y2;
      label.setAttribute("x", String(mx));
      label.setAttribute("y", String(my - 4));
      label.setAttribute("class", "ov-count");
      label.textContent = `${l.count} ${l.count === 1 ? words[0] : words[1]}`;
      svg.append(path, label);
      paths.push({ link: l, path, label });
    }
    // keep labels that land close together apart
    const placed: [number, number][] = [];
    for (const p of [...paths].sort((a, b) => Number(a.label.getAttribute("y")) - Number(b.label.getAttribute("y")))) {
      const x = Number(p.label.getAttribute("x"));
      let y = Number(p.label.getAttribute("y"));
      for (const [px, py] of placed) if (Math.abs(px - x) < 70 && Math.abs(py - y) < 13) y = py + 13;
      p.label.setAttribute("y", String(y));
      placed.push([x, y]);
    }
    if (hotItem) hot(hotItem);
  }

  const ro = new ResizeObserver(() => requestAnimationFrame(layout));
  ro.observe(board);
  requestAnimationFrame(layout);
  (document as any).fonts?.ready?.then(() => layout());

  function mark(counts: Map<OvItem, [number, number, number]> | null, info?: { label: string; detail: string; onClear(): void }): void {
    for (const [item, row] of rowEl) {
      row.querySelector(".ov-diff")?.remove();
      const c = counts?.get(item);
      row.classList.toggle("has-added", !!c && c[0] > 0 && c[1] === 0 && c[2] === 0);
      row.classList.toggle("has-diff", !!c);
      if (!c) continue;
      const tag = el("span", "ov-diff");
      if (c[0]) tag.append(el("span", "vc-add", `+${c[0]}`));
      if (c[1]) tag.append(el("span", "vc-chg", `~${c[1]}`));
      if (c[2]) tag.append(el("span", "vc-del", `−${c[2]}`));
      row.insertBefore(tag, row.querySelector(".ov-meta"));
    }
    board.classList.toggle("is-marked", !!counts?.size);
    banner.hidden = !info;
    banner.replaceChildren();
    if (info) {
      const text = el("span", "ov-banner-text");
      text.append("Changes from ", el("strong", "", info.label), ` · ${info.detail}`);
      const clear = el("button", "link-btn", "Clear");
      clear.type = "button";
      clear.onclick = () => info.onClear();
      banner.append(text, clear);
    }
    requestAnimationFrame(layout);
  }
  return { destroy: () => ro.disconnect(), mark };
}
