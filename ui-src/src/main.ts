import { Bridge, type CallToolResult, type HostContext } from "./bridge";
import {
  KINDS, KIND, LINK_GROUPS, Model, NESTED_KINDS, OPENABLE, SINGULAR, breakdown, computeVisible, isFile, isGenAI,
  neighbourhood, relPhrase, shapeOf, type GEdge, type GNode, type Group, type Kind, type Payload, type VisState,
} from "./model";
import { drawGlyph } from "./icons";
import { buildOverview, renderOverview, type OvItem, type Overview, type OverviewHandle } from "./overview";
import { marksFor, renderChanges, type DiffMarks, type History, type HistoryEntry } from "./changes";
import { GraphView } from "./view";

const $ = <T extends HTMLElement = HTMLElement>(sel: string) => document.querySelector(sel) as T;
const el = <K extends keyof HTMLElementTagNameMap>(tag: K, cls?: string, text?: string) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
};
const fmt = new Intl.NumberFormat();
const BIG_GRAPH = 900;

const bridge = new Bridge();
let model: Model | null = null;
let view: GraphView | null = null;
let vis: VisState | null = null;
let lastInput: Record<string, any> = {};
let focusLabel = "";
let mode: "overview" | "map" = "map";
let hasOverview = false;
let ovHandle: OverviewHandle | null = null;
let ovModel: Overview | null = null;
let history: History | null = null;
let marks: DiffMarks | null = null;
let changesOpen = false;

// ── host integration ──────────────────────────────────────────────────────
function applyHostContext(ctx: HostContext): void {
  const root = document.documentElement;
  const vars = ctx.styles?.variables ?? {};
  for (const [k, v] of Object.entries(vars)) if (v) root.style.setProperty(k, v);
  if (ctx.styles?.css?.fonts && !document.getElementById("host-fonts")) {
    const s = el("style");
    s.id = "host-fonts";
    s.textContent = ctx.styles.css.fonts;
    document.head.appendChild(s);
  }
  if (ctx.theme) root.dataset.theme = ctx.theme;
  root.dataset.display = ctx.displayMode ?? "inline";
  const fs = $("#fullscreen");
  const modes = ctx.availableDisplayModes ?? [];
  fs.hidden = !modes.includes("fullscreen");
  fs.textContent = ctx.displayMode === "fullscreen" ? "Exit full screen" : "Full screen";
  if (ctx.safeAreaInsets) {
    const i = ctx.safeAreaInsets;
    document.body.style.padding = `${i.top}px ${i.right}px ${i.bottom}px ${i.left}px`;
  }
  sizeToHost();
  requestAnimationFrame(() => view?.readPalette());
}

let standalone = false; // opened as its own page (artifact / browser tab) rather than inside an MCP host

function sizeToHost(): void {
  const ctx = bridge.hostContext;
  const app = $("#app");
  if (standalone) {
    // one-screen page: fill the window (100% rather than 100vh, so an artifact's safe-area padding fits)
    document.documentElement.style.height = "100%";
    document.body.style.height = "100%";
    app.style.height = "100%";
    app.style.minHeight = "480px";
  } else if (ctx.displayMode === "fullscreen") {
    app.style.height = "100vh";
  } else {
    const dims = ctx.containerDimensions ?? {};
    const cap = dims.height ?? dims.maxHeight ?? 640;
    app.style.height = `${Math.max(420, Math.min(640, cap))}px`;
  }
  if (bridge.connected) bridge.sizeChanged(app.getBoundingClientRect().height);
}

bridge.on("ui/notifications/host-context-changed", () => applyHostContext(bridge.hostContext));
bridge.on("ui/notifications/tool-input", (p) => {
  lastInput = p.arguments ?? {};
  if (!model) showLoading(lastInput);
});
bridge.on("ui/notifications/tool-input-partial", (p) => {
  if (!model && p.arguments?.repo) showLoading(p.arguments);
});
bridge.on("ui/notifications/tool-result", (p: CallToolResult) => handleResult(p));
bridge.on("ui/notifications/tool-cancelled", (p) => {
  if (!model) showMessage("Graphing was cancelled", p.reason ? `Reason: ${p.reason}.` : "Ask Claude to graph the repository again.");
});
bridge.on("ui/resource-teardown", () => ({}));

function handleResult(r: CallToolResult): void {
  const sc = r.structuredContent as Payload | undefined;
  if (r.isError || !sc || sc.error || !sc.nodes) {
    const text = sc?.error ?? r.content?.find((c) => c.type === "text")?.text ?? "The server returned no graph.";
    showMessage("Couldn't build this graph", text);
    return;
  }
  load(sc);
}

// ── states ────────────────────────────────────────────────────────────────
function showLoading(args: Record<string, any>): void {
  const repo = String(args.repo ?? "repository").replace(/^https?:\/\/(www\.)?github\.com\//, "");
  const s = $("#state");
  s.hidden = false;
  s.replaceChildren(
    el("p", "state-title", `Graphing ${repo}${args.path ? `/${args.path}` : ""}`),
    el("p", "state-body", "Downloading the source and parsing it. Most repositories take a few seconds."),
    el("div", "progress"),
  );
}

function showMessage(title: string, body: string): void {
  const s = $("#state");
  s.hidden = false;
  s.replaceChildren(el("p", "state-title", title), el("p", "state-body", body));
}

// ── load & render ─────────────────────────────────────────────────────────
function load(payload: Payload): void {
  model = new Model(payload);
  $("#state").hidden = true;
  $("#app").classList.add("ready");
  vis = {
    kinds: new Set(KINDS.map((k) => k.key)),
    groups: new Set(LINK_GROUPS.map((g) => g.key)),
    expanded: new Set(),
    collapsed: new Set(),
    focus: null,
    opened: new Set(),
  };
  for (const k of NESTED_KINDS) vis.kinds.delete(k); // an agent's steps and the env vars open on double-click
  fitToBudget(model, vis);
  focusLabel = "";
  // the overview is the calm way in; explore results and focused pages open straight on the map
  ovHandle?.destroy();
  ovHandle = null;
  const ov = buildOverview(model);
  ovModel = ov;
  hasOverview = payload.mode !== "explore" && ov.itemCount >= 2;
  if (hasOverview) {
    ovHandle = renderOverview($("#overview"), ov, String(payload.repo?.name ?? model.repoLabel), {
      glyph: (k, n) => glyph(k, n),
      open: (it) => openItem(it),
    });
  }
  // version control: open on the uncommitted work when there is some
  history = ((payload as any).history as History | undefined) ?? null;
  const working = history?.entries.find((e) => e.kind === "working" && e.diff);
  marks = working ? marksFor(model, working) : null;
  mode = hasOverview && !payload.focus ? "overview" : "map";
  applyMode();
  renderHeader();
  renderLegend();
  if (!view) {
    view = new GraphView($("#canvas"), {
      onSelect: (n) => select(n, false),
      onToggle: (n) => toggleNode(n),
      onHover: (n, x, y) => hoverTip(n, x, y),
    });
    view.emphasis = (n) => (vis?.opened.has(n.id) ? breakdown(n) : []);
  }
  view.select(null);
  view.inset = { right: 0, bottom: 0 };
  closePanel();
  closeChanges();
  refresh(true);
  applyMarks();
  const focus = payload.focus ? model.byId.get(payload.focus) : null;
  if (focus && OPENABLE.has(focus.kind) && breakdown(focus).length) vis.opened.add(focus.id); // explore an agent → its breakdown
  if (focus && payload.focusDepth) {
    // whole graph shipped with a focus: show the neighbourhood, keep "Show whole repo" local
    setTimeout(() => {
      focusOn(focus, payload.focusDepth);
      select(focus, false);
      if (view) { view.fit(true); view.autoFit = true; }
    }, 60);
  } else if (focus) {
    focusLabel = labelOf(focus);
    renderHeader();
    // an explore result is small: open the details and fit the whole neighbourhood beside them
    setTimeout(() => {
      select(focus, false);
      if (view) { view.fit(true); view.autoFit = true; }
    }, 60);
  }
}

/** Large repos open as a readable overview: hide the least structural kinds first, then fold the
 *  deepest/biggest folders into themselves until the map fits the node budget. */
let autoFolded = 0;
let autoHidden: Kind[] = [];
function fitToBudget(m: Model, v: VisState): void {
  autoFolded = 0;
  autoHidden = [];
  if (m.nodes.length <= BIG_GRAPH || m.payload.mode === "explore") return;
  const drop = (kinds: Kind[]) => {
    for (const k of kinds) {
      if (computeVisible(m, v).nodes.length <= BIG_GRAPH) return;
      if (v.kinds.delete(k)) autoHidden.push(k);
    }
  };
  drop(["Function", "DocumentationFile"]); // cheapest to lose in an overview
  foldFolders(m, v);
  drop(["Class", "TemplateMarkupFile", "TestingFile"]);
}

function foldFolders(m: Model, v: VisState): void {
  const shown = new Set(computeVisible(m, v).nodes);
  let count = shown.size;
  if (count <= BIG_GRAPH) return;
  const below = new Map<GNode, GNode[]>();
  const stack: GNode[] = [];
  for (const n of m.nodes) if (n.kind === "Folder" && !n.props.is_root) stack.push(n);
  const descendants = (f: GNode): GNode[] => {
    const out: GNode[] = [];
    const todo = [...f.children];
    while (todo.length) {
      const c = todo.pop()!;
      out.push(c);
      todo.push(...c.children);
    }
    return out;
  };
  for (const f of stack) below.set(f, descendants(f).filter((d) => shown.has(d)));
  stack.sort((a, b) => b.depth - a.depth || below.get(b)!.length - below.get(a)!.length);
  const hidden = new Set<GNode>();
  for (const f of stack) {
    if (count <= BIG_GRAPH) break;
    if (f.depth < 1 || hidden.has(f)) continue;
    const fresh = below.get(f)!.filter((d) => !hidden.has(d));
    if (!fresh.length) continue;
    v.collapsed.add(f.id);
    autoFolded++;
    for (const d of fresh) hidden.add(d);
    count -= fresh.length;
  }
}

function refresh(reseed = false): void {
  if (!model || !vis || !view) return;
  const { nodes, links } = computeVisible(model, vis);
  view.collapsed = vis.collapsed;
  view.setData(nodes, links, { reseed });
  renderStatus(nodes.length);
  renderLegend();
}

// ── version control ─────────────────────────────────────────────────────────
function pickEntry(e: HistoryEntry | null): void {
  if (!model) return;
  marks = e ? marksFor(model, e) : null;
  applyMarks();
}

/** Put the picked entry's marks on the overview rows and the map, and refresh the chip and panel. */
function applyMarks(): void {
  if (!model) return;
  view?.setDiff(marks ? { added: marks.added, changed: marks.changed } : null);
  if (ovHandle && ovModel) {
    const ov = ovModel;
    if (!marks || !marks.entry.diff) ovHandle.mark(null);
    else {
      const counts = new Map<OvItem, [number, number, number]>();
      const d = marks.entry.diff;
      const symbolPaths = new Set([...d.added, ...d.changed, ...d.removed]
        .filter((r) => !/File$/.test(r.l)).map((r) => r.p));
      ([[d.added, 0], [d.changed, 1], [d.removed, 2]] as const).forEach(([list, i]) => {
        for (const r of list) {
          if (/File$/.test(r.l) && i === 1 && symbolPaths.has(r.p)) continue; // its functions say it better
          const n = model!.byId.get(r.id);
          const it = (n && ov.itemOf(n)) || ov.itemOfPath(r.p);
          if (!it) continue;
          const c = counts.get(it) ?? [0, 0, 0];
          c[i]++;
          counts.set(it, c);
        }
      });
      const e = marks.entry;
      ovHandle.mark(counts, {
        label: e.kind === "working" ? "uncommitted work" : `${e.short} ${e.title}`,
        detail: e.summary,
        onClear: () => pickEntry(null),
      });
    }
  }
  renderVcChip();
  if (changesOpen) drawChanges();
}

function renderVcChip(): void {
  const chip = $("#vc");
  chip.hidden = !history;
  $("#ref").hidden = !!history;
  if (!history) return;
  const h = history;
  chip.replaceChildren();
  chip.append(el("span", "vc-branch", `${h.branch} @ ${h.short}`));
  const working = h.entries.find((e) => e.kind === "working");
  if (working) chip.append(el("span", "vc-badge is-dirty", `● ${working.files.length} uncommitted`));
  if (h.upstream && h.ahead) chip.append(el("span", "vc-badge is-ahead", `↑${h.ahead} not pushed`));
  if (!working && !(h.upstream && h.ahead)) chip.append(el("span", "vc-badge", `${h.entries.length} commits`));
  chip.setAttribute("aria-expanded", String(changesOpen));
  chip.title = "Uncommitted changes and recent commits";
}

function drawChanges(): void {
  if (!model || !history) return;
  const host = $("#changes");
  renderChanges(host, history, model, marks, {
    glyph: (k, n) => glyph(k, n),
    pick: (e) => pickEntry(e),
    openNode: (n) => { setMode("map"); select(n, true); },
    openLink: (url) => void openLink(url),
    close: () => closeChanges(),
    showInMap: () => showChangesInMap(),
  });
}

function openChanges(): void {
  if (!history) return;
  changesOpen = true;
  const narrow = window.matchMedia("(max-width: 640px)").matches;
  if (narrow) closePanel();
  $("#changes").hidden = false;
  $("#app").classList.toggle("changes-open", !narrow);
  if (view && !narrow) view.inset = { ...view.inset, left: 360 };
  drawChanges();
  renderVcChip();
}

function closeChanges(): void {
  changesOpen = false;
  $("#changes").hidden = true;
  $("#app").classList.remove("changes-open");
  if (view) view.inset = { ...view.inset, left: 0 };
  if (history) renderVcChip();
}

/** The map, narrowed to what the picked entry touched and what touches it. */
function showChangesInMap(): void {
  if (!model || !vis || !marks) return;
  const touched = [...marks.added, ...marks.changed];
  if (!touched.length) return;
  const keep = new Set<string>();
  for (const n of touched) {
    keep.add(n.id);
    for (const e of [...n.outE, ...n.inE]) if (e.group !== "contains") { keep.add(e.s.id); keep.add(e.t.id); }
    if (n.fileOf) vis.expanded.add(n.fileOf.id);
  }
  for (const n of marks.affected) keep.add(n.id);
  for (const id of [...keep]) for (let a = model.byId.get(id)?.parent; a; a = a.parent) { keep.add(a.id); vis.collapsed.delete(a.id); }
  vis.focus = keep;
  focusLabel = marks.entry.kind === "working" ? "uncommitted changes" : `changes in ${marks.entry.short}`;
  setMode("map");
  select(null, false);
  renderHeader();
  refresh(true);
}

// ── overview ⇄ map ────────────────────────────────────────────────────────
function applyMode(): void {
  $("#overview").hidden = mode !== "overview";
  $("#app").dataset.mode = mode;
  $("#modes").hidden = !hasOverview;
  document.querySelectorAll<HTMLButtonElement>("#modes button").forEach((b) => {
    b.setAttribute("aria-selected", String(b.dataset.mode === mode));
  });
  $("#scope-overview").hidden = !hasOverview;
  renderHeader();
}

function setMode(m: "overview" | "map"): void {
  if (m === mode) return;
  if (m === "overview") select(null, false);
  mode = m;
  applyMode();
}

/** A row of the overview opens just that part of the graph. */
function openItem(it: OvItem): void {
  if (!model || !vis || !view) return;
  setMode("map");
  vis.opened.clear(); // each row opens on its own, not on top of the last one
  if (it.node) {
    const n = it.node;
    if (OPENABLE.has(n.kind) && breakdown(n).length) vis.opened.add(n.id);
    focusOn(n, 1);
    select(n, false);
    return;
  }
  if (it.overflow || !it.members.length) { void showAll(); return; }
  // a folder group: its files (and their code when that stays small), plus what they link to
  const inside: GNode[] = [];
  const stack = [...it.members];
  while (stack.length) {
    const n = stack.pop()!;
    inside.push(n);
    stack.push(...n.children);
  }
  const code = inside.filter((n) => n.kind === "Function" || n.kind === "Class").length;
  const detail = code <= 40;
  const shown = (n: GNode): GNode => (!detail && (n.kind === "Function" || n.kind === "Class") && n.fileOf ? n.fileOf : n);
  const keep = new Set<string>();
  for (const n of inside) keep.add(shown(n).id);
  for (const n of inside) {
    for (const e of [...n.outE, ...n.inE]) {
      if (e.group === "contains") continue;
      keep.add(shown(e.s).id);
      keep.add(shown(e.t).id);
    }
  }
  for (const id of [...keep]) for (let a = model.byId.get(id)?.parent; a; a = a.parent) keep.add(a.id);
  for (const n of inside) for (let a = n.parent; a; a = a.parent) vis.collapsed.delete(a.id);
  for (const f of it.members) if (detail) vis.expanded.add(f.id); else vis.expanded.delete(f.id);
  vis.focus = keep;
  focusLabel = (it.prefix ?? "") + it.label;
  select(null, false);
  renderHeader();
  refresh(true);
}

function renderHeader(): void {
  if (!model) return;
  const repo = model.payload.repo ?? {};
  const title = $("#repo");
  title.textContent = model.repoLabel;
  const url = repo.url as string | undefined;
  title.onclick = url ? (e) => { e.preventDefault(); void openLink(url); } : null;
  title.setAttribute("href", url ?? "#");
  title.classList.toggle("is-link", !!url);
  const ref = $("#ref");
  const sha = String(repo.sha ?? "");
  ref.textContent = [repo.ref, sha && sha !== "local" ? sha.slice(0, 7) : ""].filter(Boolean).join(" at ");
  const st = model.payload.stats ?? {};
  const meta = $("#meta");
  const nodes = model.payload.totalNodes ?? st.nodes ?? model.nodes.length;
  const edges = model.payload.totalEdges ?? st.edges ?? model.edges.length;
  const parts = [`${fmt.format(nodes)} nodes and ${fmt.format(edges)} links`];
  if (st.build_ms !== undefined) parts.push(`parsed in ${(st.build_ms / 1000).toFixed(st.build_ms < 1000 ? 2 : 1)} s`);
  meta.textContent = parts.join(", ");
  const scope = $("#scope");
  const exploring = mode === "map" && (model.payload.mode === "explore" || !!vis?.focus);
  scope.hidden = !exploring;
  if (exploring) {
    $("#scope-label").textContent = focusLabel ? `Around ${focusLabel}` : "Partial view";
  }
}

function renderStatus(visibleCount: number): void {
  if (!model || !vis) return;
  const notes: string[] = [];
  const stillHidden = autoHidden.filter((k) => !vis!.kinds.has(k));
  const folded = [...vis.collapsed].length;
  if (stillHidden.length || folded) {
    const kinds = stillHidden.map((k) => KIND[k].name.toLowerCase());
    const what = [
      kinds.length ? `${kinds.join(", ")} hidden` : "",
      folded ? `${fmt.format(folded)} folder${folded > 1 ? "s" : ""} folded` : "",
    ].filter(Boolean).join(" and ");
    notes.push(`Overview of a large repo: ${what}. Double-click a folder or file to open it, or turn kinds on below.`);
  } else if (vis.focus && mode === "map" && [...vis.focus].every((id) => model!.byId.get(id)?.kind !== "Function")) {
    notes.push("Showing files only. Double-click a file to open its functions.");
  } else if (!vis.kinds.has("Function") && model.kindCounts.get("Function")) {
    notes.push("Functions are hidden. Double-click a file to show its functions.");
  }
  if (model.kindCounts.get("GenAI") && !vis.opened.size) {
    notes.push("Double-click an agent, MCP server, service or env var to break it down into the code behind it.");
  }
  if (model.payload.truncated) notes.push(`Showing the ${fmt.format(model.nodes.length)} most connected of ${fmt.format(model.payload.totalNodes ?? 0)} nodes.`);
  if (!visibleCount) notes.push("Everything is filtered out. Turn some kinds back on below.");
  $("#status").textContent = notes.join(" ");
}

// ── legend / filters ──────────────────────────────────────────────────────
function glyph(kind: Kind, n?: GNode): HTMLCanvasElement {
  const size = 16;
  const c = el("canvas", "glyph");
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  c.width = c.height = size * dpr;
  const ctx = c.getContext("2d")!;
  ctx.scale(dpr, dpr);
  const cs = getComputedStyle(document.documentElement);
  const shape = n ? shapeOf(n) : KIND[kind].shape;
  drawGlyph(ctx, shape, size, cs.getPropertyValue(KIND[kind].color).trim(), cs.getPropertyValue("--on-kind").trim());
  return c;
}

function renderLegend(): void {
  if (!model || !vis) return;
  // only what's in scope: a focused view lists the few kinds it actually shows
  const scope = vis.focus;
  const kindCounts = new Map<Kind, number>();
  for (const n of model.nodes) if (!scope || scope.has(n.id)) kindCounts.set(n.kind, (kindCounts.get(n.kind) ?? 0) + 1);
  const groupCounts = new Map<Group, number>();
  for (const e of model.edges) {
    if (scope && !(scope.has(e.s.id) && scope.has(e.t.id))) continue;
    groupCounts.set(e.group, (groupCounts.get(e.group) ?? 0) + 1);
  }
  const kinds = $("#kinds");
  kinds.replaceChildren();
  for (const k of KINDS) {
    const count = kindCounts.get(k.key) ?? 0;
    if (!count || k.chip === false) continue;
    const b = el("button", "chip");
    b.dataset.kind = k.key;
    b.setAttribute("aria-pressed", String(vis.kinds.has(k.key)));
    b.append(glyph(k.key), el("span", "chip-name", k.name), el("span", "chip-count", fmt.format(count)));
    b.onclick = () => {
      if (!vis) return;
      if (vis.kinds.has(k.key)) vis.kinds.delete(k.key);
      else vis.kinds.add(k.key);
      b.setAttribute("aria-pressed", String(vis.kinds.has(k.key)));
      refresh();
    };
    kinds.appendChild(b);
  }
  const groups = $("#groups");
  groups.replaceChildren();
  for (const g of LINK_GROUPS) {
    const count = groupCounts.get(g.key) ?? 0;
    if (!count) continue;
    const b = el("button", "chip chip-link");
    b.dataset.group = g.key;
    b.setAttribute("aria-pressed", String(vis.groups.has(g.key)));
    const swatch = el("span", `swatch swatch-${g.key}`);
    b.append(swatch, el("span", "chip-name", g.name), el("span", "chip-count", fmt.format(count)));
    b.onclick = () => {
      if (!vis) return;
      if (vis.groups.has(g.key as Group)) vis.groups.delete(g.key as Group);
      else vis.groups.add(g.key as Group);
      b.setAttribute("aria-pressed", String(vis.groups.has(g.key as Group)));
      refresh();
    };
    groups.appendChild(b);
  }
}

// ── selection, panel ──────────────────────────────────────────────────────
function labelOf(n: GNode): string {
  if (n.kind === "Function" || n.kind === "Class") return n.props.qualname ?? n.name;
  if (isGenAI(n.kind)) return n.name;
  if (n.kind === "Folder") return n.props.is_root ? n.name : `${n.path || n.name}/`;
  return n.kind === "ApiEndpoint" ? n.name : n.path || n.name;
}

function ensureVisible(n: GNode): void {
  if (!vis) return;
  let changed = false;
  if ((n.kind === "Function" || n.kind === "Class") && !vis.kinds.has(n.kind) && n.fileOf && !vis.expanded.has(n.fileOf.id)) {
    vis.expanded.add(n.fileOf.id);
    changed = true;
  }
  if (NESTED_KINDS.has(n.kind) && !vis.kinds.has(n.kind) && n.parent && !vis.opened.has(n.parent.id)) {
    vis.opened.add(n.parent.id);
    changed = true;
  } else if (!vis.kinds.has(n.kind) && !(n.kind === "Function" || n.kind === "Class") && !NESTED_KINDS.has(n.kind)) {
    vis.kinds.add(n.kind);
    changed = true;
  }
  for (let a = n.parent; a; a = a.parent) if (vis.collapsed.delete(a.id)) changed = true;
  if (vis.focus && !vis.focus.has(n.id)) { vis.focus = null; changed = true; renderHeader(); }
  if (changed) refresh();
}

function select(n: GNode | null, center: boolean): void {
  if (!view || !model) return;
  if (n) ensureVisible(n);
  view.select(n);
  if (!n) { closePanel(); return; }
  openPanel(n);
  const panel = $("#panel").getBoundingClientRect();
  const stage = $("#canvas").getBoundingClientRect();
  const sheet = panel.width > stage.width * 0.8; // narrow layout: panel is a bottom sheet
  view.inset = sheet ? { right: 0, bottom: panel.height + 8 } : { right: panel.width + 10, bottom: 0, left: view.inset.left };
  if (center) view.centerOn(n);
  if (bridge.connected && bridge.hostCapabilities.updateModelContext) {
    const where = n.path ? ` in ${n.path}${n.props.line ? `:${n.props.line}` : ""}` : "";
    void bridge.updateModelContext(
      `In the repo graph for ${model.repoLabel} (graph_id ${model.payload.graphId}), the user has selected ${SINGULAR[n.kind]} ` +
      `${labelOf(n)}${where} (node id ${n.id}).`,
    ).catch(() => {});
  }
}

function toggleNode(n: GNode): void {
  if (!vis) return;
  if (isFile(n.kind)) {
    if (vis.expanded.has(n.id)) vis.expanded.delete(n.id);
    else {
      vis.expanded.add(n.id);
      if (vis.focus) { // a focused view grows to include the file's code and what it links to
        const todo = [...n.children];
        while (todo.length) {
          const c = todo.pop()!;
          vis.focus.add(c.id);
          for (const e of [...c.outE, ...c.inE]) if (e.group !== "contains") { vis.focus.add(e.s.id); vis.focus.add(e.t.id); }
          todo.push(...c.children);
        }
        for (const id of [...vis.focus]) for (let a = model?.byId.get(id)?.parent; a; a = a.parent) vis.focus.add(a.id);
      }
    }
    refresh();
  } else if ((n.kind === "Folder" && !n.props.is_root) || n.kind === "GenAI") {
    if (vis.collapsed.has(n.id)) vis.collapsed.delete(n.id);
    else vis.collapsed.add(n.id);
    refresh();
  } else if (OPENABLE.has(n.kind)) {
    if (vis.opened.has(n.id)) vis.opened.delete(n.id);
    else vis.opened.add(n.id);
    refresh();
    select(n, false);
    // frame the node together with what it broke down into, once they've moved into place
    if (vis.opened.has(n.id) && view) {
      const parts = [n, ...breakdown(n)];
      setTimeout(() => view?.fit(true, parts.filter((p) => p.x !== undefined)), 420);
    }
  } else {
    select(n, true);
  }
}

const isOpenable = (n: GNode) => OPENABLE.has(n.kind) && breakdown(n).length > 0;

/** Agent: its steps in order, where each goes next and what it runs. */
function flowSection(n: GNode): HTMLElement | null {
  const steps = n.children.filter((c) => c.kind === "AgentStep").sort((a, b) => (a.props.order ?? 0) - (b.props.order ?? 0));
  if (!steps.length) return null;
  const sec = el("section", "rel");
  sec.append(el("h3", "rel-title", `Flow (${steps.length} step${steps.length > 1 ? "s" : ""})`));
  const ol = el("ol", "flow-list");
  for (const st of steps) {
    const li = el("li", "flow-step");
    const b = el("button", "rel-item");
    b.append(glyph(st.kind, st), el("span", "rel-name", st.name));
    if (st.props.entry) b.append(el("span", "flow-badge", "start"));
    b.onclick = () => select(st, true);
    li.append(b);
    const nexts = st.outE.filter((e) => e.rel === "NEXT");
    const runs = st.outE.filter((e) => e.rel === "RUNS").map((e) => e.t);
    const tools = st.outE.filter((e) => e.rel === "USES_TOOL").map((e) => e.t);
    const lines: string[] = [];
    if (runs.length) lines.push(`runs ${runs.map((r) => r.kind === "Agent" ? `${r.name} (sub-agent)` : `${r.name}()`).join(", ")}`);
    else if (st.props.kind === "tools") lines.push(`runs the agent's tools${tools.length ? `: ${tools.map((t) => t.name).join(", ")}` : ""}`);
    else if (st.props.runs) lines.push(`runs ${st.props.runs} (not found in the code graph)`);
    const to = nexts.map((e) => e.t.name + (e.props?.kind === "conditional" ? " (if)" : e.props?.kind === "goto" ? " (goto)" : ""));
    if (st.props.can_end) to.push("END");
    if (to.length) lines.push(`then → ${to.join(", ")}`);
    if (lines.length) li.append(el("p", "flow-note", lines.join(" · ")));
    ol.append(li);
  }
  sec.append(ol);
  return sec;
}

function openPanel(n: GNode): void {
  if (!model) return;
  const p = $("#panel");
  p.hidden = false;
  const body = el("div", "panel-body");
  const kind = el("p", "panel-kind");
  const kindText = n.kind === "ExternalService" && n.props.category ? `${n.props.category} service`
    : n.kind === "Function" && n.props.ai_tool ? `Function, used as ${String(n.props.ai_tool).replace(/^(MCP|agent|LangChain)/, (m) => m === "agent" ? "an agent" : m === "MCP" ? "an MCP" : "a LangChain")}`
    : SINGULAR[n.kind] + (n.props.defined_in_repo === false ? ", not defined in this repo" : "");
  kind.append(glyph(n.kind, n), document.createTextNode(kindText));
  const title = el("h2", `panel-title ${["Function", "Class", "ApiEndpoint", "AgentStep", "EnvVar"].includes(n.kind) ? "is-code" : ""}`, labelOf(n));
  body.append(kind, title);
  if (n.props.warning) body.append(el("p", "panel-warn", String(n.props.warning)));

  if (n.path && n.kind !== "Folder") {
    const loc = el("p", "panel-loc");
    const text = n.path + (n.props.line ? `:${n.props.line}` : "");
    const url = model.fileUrl(n);
    if (url) {
      const a = el("a", "link", text);
      a.href = url;
      a.onclick = (e) => { e.preventDefault(); void openLink(url); };
      loc.append(a);
    } else loc.textContent = text;
    body.append(loc);
  }
  if (n.props.signature) body.append(el("pre", "panel-sig", String(n.props.signature)));
  if (n.props.summary) body.append(el("p", "panel-summary", String(n.props.summary)));
  const facts: string[] = [];
  if (n.props.framework) facts.push(String(n.props.framework));
  if (n.kind === "Agent" && n.props.steps) facts.push(`${n.props.steps} steps`);
  if (n.kind === "McpServer") {
    const parts = [["tools", n.props.tools], ["resources", n.props.resources], ["prompts", n.props.prompts]]
      .filter(([, c]) => c).map(([w, c]) => `${c} ${c === 1 ? String(w).slice(0, -1) : w}`);
    if (parts.length) facts.push(parts.join(", "));
    if (n.props.transport) facts.push(`${n.props.transport} transport`);
  }
  if (n.kind === "EnvVar") {
    facts.push(n.props.mode === "required" ? "Required: the app fails without it" : n.props.mode === "default" ? "Has a default" : "Optional: empty when unset");
    if (n.props.reads) facts.push(`read in ${n.props.reads} place${n.props.reads > 1 ? "s" : ""}`);
  }
  if (n.kind === "EnvGroup") facts.push(`${n.props.count} variables, ${n.props.required ?? 0} required`);
  if (n.kind === "ExternalService" && n.props.sdks?.length) facts.push(`via ${n.props.sdks.slice(0, 3).join(", ")}`);
  if (n.kind === "AgentStep" && n.props.kind === "tools") facts.push("tool-calling step");
  if (n.props.test_framework && n.props.test_framework !== "None") facts.push(String(n.props.test_framework));
  if (n.props.language) facts.push(String(n.props.language));
  if (n.props.loc) facts.push(`${fmt.format(n.props.loc)} lines`);
  if (n.kind === "Folder" && n.props.file_count !== undefined) facts.push(`${fmt.format(n.props.file_count)} files directly inside`);
  if (n.props.async) facts.push("async");
  if (facts.length) body.append(el("p", "panel-facts", facts.join(", ")));

  if (n.kind === "Agent") {
    const flow = flowSection(n);
    if (flow) body.append(flow);
  }
  const unresolved = [...(n.props.unresolved ?? []), ...(n.props.tools_unresolved ?? []).map((t: string) => `tool ${t}`)];
  if (unresolved.length) {
    const sec = el("section", "rel");
    sec.append(el("h3", "rel-title", "Not linked to code"), el("p", "panel-note", unresolved.slice(0, 8).join("; ") + "."));
    body.append(sec);
  }

  // relationships grouped by how they read from this node
  const groups = new Map<string, { o: GNode; e: GEdge }[]>();
  for (const e of n.outE) {
    if (n.kind === "Agent" && e.rel === "BELONGS_TO" && e.t.kind === "GenAI") continue;
    const k = relPhrase(e.rel, "out");
    (groups.get(k) ?? groups.set(k, []).get(k)!).push({ o: e.t, e });
  }
  for (const e of n.inE) {
    if (n.kind === "Agent" && e.s.kind === "AgentStep" && e.rel === "BELONGS_TO") continue; // shown as the flow
    const k = relPhrase(e.rel, "in");
    (groups.get(k) ?? groups.set(k, []).get(k)!).push({ o: e.s, e });
  }
  const order = ["Runs", "Decided by", "Uses tools", "Exposes", "Goes to", "Comes after", "Run by", "Decides for", "Tool of", "Exposed by",
    "Uses", "Used by", "Reads", "Read by", "Called by", "Calls", "Requested by", "Requests", "Tested by", "Tests", "Imported by", "Imports",
    "Extends", "Extended by", "Contains", "Inside"];
  for (const key of order) {
    const list = groups.get(key);
    if (!list?.length) continue;
    const seen = new Set<GNode>();
    const uniq = list.filter(({ o }) => !seen.has(o) && !!seen.add(o));
    const sec = el("section", "rel");
    sec.append(el("h3", "rel-title", `${key} (${fmt.format(uniq.length)})`));
    const ul = el("ul", "rel-list");
    for (const { o, e } of uniq.slice(0, 40)) {
      const li = el("li");
      const b = el("button", "rel-item");
      b.append(glyph(o.kind, o), el("span", "rel-name", labelOf(o)));
      const where = !e.props?.line ? "" : e.props.file && e.props.file !== o.path ? `${e.props.file}:${e.props.line}` : `line ${e.props.line}`;
      if (where) {
        const tip = [e.props?.how, e.props?.basis === "inferred" ? "inferred" : ""].filter(Boolean).join(" · ");
        const span = el("span", "rel-path", where);
        if (tip) span.title = tip;
        b.append(span);
      } else if ((o.kind === "Function" || o.kind === "Class") && o.path) b.append(el("span", "rel-path", o.path));
      b.onclick = () => select(o, true);
      li.append(b);
      ul.append(li);
    }
    if (uniq.length > 40) ul.append(el("li", "rel-more", `${fmt.format(uniq.length - 40)} more not listed`));
    sec.append(ul);
    body.append(sec);
  }

  const actions = el("div", "panel-actions");
  if (isOpenable(n)) {
    const open = el("button", "btn", vis?.opened.has(n.id) ? "Fold back" : n.kind === "Agent" ? "Break down into steps and code"
      : n.kind === "ExternalService" ? "Show the code that uses it" : n.kind === "EnvVar" ? "Show where it's read"
      : n.kind === "EnvGroup" ? "Show all variables" : "Show its parts");
    open.onclick = () => { toggleNode(n); openPanel(n); };
    actions.append(open);
  }
  const near = el("button", "btn", "Show only its neighbours");
  near.onclick = () => focusOn(n);
  actions.append(near);
  if (isFile(n.kind) && n.children.some((c) => c.kind === "Function" || c.kind === "Class")) {
    const open = el("button", "btn", vis?.expanded.has(n.id) ? "Fold its functions" : "Show its functions");
    open.onclick = () => { toggleNode(n); openPanel(n); };
    actions.append(open);
  }
  if (bridge.connected && bridge.hostCapabilities.message) {
    const ask = el("button", "btn btn-primary", "Ask Claude about this");
    ask.onclick = () => {
      const where = n.path ? ` (${n.path}${n.props.line ? `:${n.props.line}` : ""})` : "";
      void bridge.sendMessage(
        `Explain ${SINGULAR[n.kind].toLowerCase()} \`${labelOf(n)}\`${where} in ${model!.repoLabel}: what it does, ` +
        `what depends on it and what it depends on. Use the repo graph (graph_id ${model!.payload.graphId}).`,
      ).catch(() => {});
    };
    actions.append(ask);
  }
  body.append(actions);
  p.replaceChildren(panelHeader(), body);
  p.scrollTop = 0;
}

function panelHeader(): HTMLElement {
  const h = el("div", "panel-head");
  const close = el("button", "icon-btn", "Close");
  close.setAttribute("aria-label", "Close details");
  close.onclick = () => select(null, false);
  h.append(close);
  return h;
}

function closePanel(): void {
  $("#panel").hidden = true;
  if (view) view.inset = { right: 0, bottom: 0, left: view.inset.left };
}

function focusOn(n: GNode, depth = 1): void {
  if (!model || !vis || !view) return;
  vis.focus = neighbourhood(model, n, depth);
  vis.expanded.add(n.fileOf?.id ?? n.id);
  for (const id of vis.focus) {
    const o = model.byId.get(id);
    if (o && (o.kind === "Function" || o.kind === "Class") && o.fileOf) vis.expanded.add(o.fileOf.id);
  }
  focusLabel = labelOf(n);
  renderHeader();
  refresh(true);
  view.select(n);
}

async function showAll(): Promise<void> {
  if (!model || !vis) return;
  if (model.payload.mode === "explore" && bridge.connected) {
    $("#scope-reset").textContent = "Loading the whole repo";
    try {
      const r = await bridge.callTool("graph_view_data", { graph: model.payload.graphId });
      handleResult(r);
    } catch (e) {
      $("#status").textContent = `Couldn't load the full graph: ${(e as Error).message}`;
    } finally {
      $("#scope-reset").textContent = "Show whole repo";
    }
    return;
  }
  vis.focus = null;
  focusLabel = "";
  renderHeader();
  refresh(true);
}

async function openLink(url: string): Promise<void> {
  if (bridge.connected) {
    try { await bridge.openLink(url); return; } catch { /* fall through */ }
  }
  window.open(url, "_blank", "noopener");
}

// ── hover tooltip ─────────────────────────────────────────────────────────
function hoverTip(n: GNode | null, x: number, y: number): void {
  const tip = $("#tip");
  if (!n || n === view?.selected) { tip.hidden = true; return; }
  tip.hidden = false;
  tip.replaceChildren(el("span", "tip-kind", SINGULAR[n.kind]), el("span", "tip-name", labelOf(n)));
  const host = $("#canvas").getBoundingClientRect();
  const left = Math.min(x + 14, host.width - 260);
  tip.style.transform = `translate(${Math.max(8, left)}px, ${Math.max(8, y - 34)}px)`;
}

// ── search ────────────────────────────────────────────────────────────────
function setupSearch(): void {
  const input = $<HTMLInputElement>("#search");
  const list = $("#results");
  let items: GNode[] = [];
  let active = 0;
  const render = () => {
    list.replaceChildren();
    list.hidden = !items.length;
    input.setAttribute("aria-expanded", String(!!items.length));
    items.forEach((n, i) => {
      const li = el("li", "result");
      li.id = `res-${i}`;
      li.setAttribute("role", "option");
      li.setAttribute("aria-selected", String(i === active));
      li.append(glyph(n.kind), el("span", "result-name", labelOf(n)));
      if (n.kind === "Function" || n.kind === "Class" || n.kind === "ApiEndpoint") li.append(el("span", "result-path", n.path));
      li.onmousedown = (e) => { e.preventDefault(); pick(n); };
      list.append(li);
    });
    if (items.length) input.setAttribute("aria-activedescendant", `res-${active}`);
  };
  const pick = (n: GNode) => {
    items = [];
    render();
    input.blur();
    setMode("map");
    select(n, true);
  };
  input.addEventListener("input", () => {
    const q = input.value.trim().toLowerCase();
    active = 0;
    if (!model || !q) { items = []; render(); return; }
    const scored: [number, GNode][] = [];
    for (const n of model.nodes) {
      const name = n.name.toLowerCase();
      const qual = String(n.props.qualname ?? n.props.handler ?? "").toLowerCase();
      const path = n.path.toLowerCase();
      let s = -1;
      if (name === q || qual === q) s = 0;
      else if (name.startsWith(q)) s = 1;
      else if (name.includes(q) || qual.includes(q)) s = 2;
      else if (path.includes(q)) s = 3;
      if (s >= 0) scored.push([s * 10 + (n.kind === "Function" ? 1 : 0) - Math.min(5, n.deg / 5), n]);
    }
    scored.sort((a, b) => a[0] - b[0]);
    items = scored.slice(0, 8).map((x) => x[1]);
    render();
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") { active = Math.min(items.length - 1, active + 1); render(); e.preventDefault(); }
    else if (e.key === "ArrowUp") { active = Math.max(0, active - 1); render(); e.preventDefault(); }
    else if (e.key === "Enter" && items[active]) { pick(items[active]); e.preventDefault(); }
    else if (e.key === "Escape") { input.value = ""; items = []; render(); }
  });
  input.addEventListener("blur", () => setTimeout(() => { items = []; render(); }, 120));
}

// ── boot ──────────────────────────────────────────────────────────────────
function wire(): void {
  setupSearch();
  $("#fit").onclick = () => view?.fit();
  $("#zoom-in").onclick = () => view?.zoomBy(1.3);
  $("#zoom-out").onclick = () => view?.zoomBy(1 / 1.3);
  $("#scope-reset").onclick = () => void showAll();
  $("#scope-overview").onclick = () => setMode("overview");
  $("#vc").onclick = () => (changesOpen ? closeChanges() : openChanges());
  document.querySelectorAll<HTMLButtonElement>("#modes button").forEach((b) => {
    b.onclick = () => setMode(b.dataset.mode as "overview" | "map");
  });
  $("#fullscreen").onclick = async () => {
    const next = bridge.hostContext.displayMode === "fullscreen" ? "inline" : "fullscreen";
    try {
      const r = await bridge.requestDisplayMode(next);
      bridge.hostContext.displayMode = (r?.mode as HostContext["displayMode"]) ?? next;
      applyHostContext(bridge.hostContext);
    } catch { /* host refused */ }
  };
  document.addEventListener("keydown", (e) => {
    const target = e.target as HTMLElement;
    if (e.key === "/" && target.tagName !== "INPUT") { $("#search").focus(); e.preventDefault(); }
    if (e.key === "Escape" && target.tagName !== "INPUT") select(null, false);
  });
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => view?.readPalette());
  window.addEventListener("resize", () => bridge.connected && sizeToHost());
}

// read-only handle for automated checks of the published page
(window as any).__repoGraph = {
  screenOf(id: string) {
    const n = model?.byId.get(id);
    if (!n || !view || n.x === undefined) return null;
    const r = $("#canvas").getBoundingClientRect();
    return { x: r.left + n.x * view.k + view.tx, y: r.top + n.y! * view.k + view.ty, visible: view.nodes.includes(n) };
  },
  visibleIds: () => view?.nodes.map((n) => n.id) ?? [],
};

async function boot(): Promise<void> {
  wire();
  const injected = (window as any).__REPO_GRAPH_PAYLOAD__ as Payload | undefined;
  if (!bridge.embedded || injected) {
    standalone = true;
    sizeToHost();
    if (injected) load(injected);
    else showMessage("Nothing to show yet", "This view opens inside Claude when the repo-graph tool runs.");
    return;
  }
  showMessage("Connecting", "Waiting for the chat to hand over the graph.");
  try {
    await bridge.connect();
    applyHostContext(bridge.hostContext);
    if (!model && lastInput.repo) showLoading(lastInput);
    else if (!model) showLoading({ repo: "repository" });
  } catch (e) {
    showMessage("Couldn't connect to the chat", `${(e as Error).message}. Reload the conversation to try again.`);
  }
}

void boot();
