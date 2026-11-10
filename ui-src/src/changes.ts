// Version control in the view: uncommitted changes and recent commits, each a set of graph changes.
// Picking one marks what it added, changed and removed on the overview and the map.
import type { GNode, Kind, Model } from "./model";

export interface DiffRec { id: string; l: Kind; n: string; p: string; q?: string; line?: number; sig?: boolean }
export interface Diff {
  added: DiffRec[];
  changed: DiffRec[];
  removed: DiffRec[];
  counts: Record<string, [number, number, number]>;
  links: { added: number; removed: number };
  truncated?: boolean;
}
export interface HistoryEntry {
  id: string;
  kind: "working" | "commit";
  title: string;
  sha?: string;
  short?: string;
  author?: string;
  date?: string;
  pushed?: boolean;
  url?: string;
  files: { path: string; status: "added" | "modified" | "deleted" }[];
  diff: Diff | null;
  summary: string;
}
export interface History {
  head: string;
  short: string;
  branch: string;
  upstream: string | null;
  ahead: number;
  behind: number;
  dirty: boolean;
  head_url?: string;
  entries: HistoryEntry[];
}

/** The marks one entry puts on the graph. */
export interface DiffMarks {
  entry: HistoryEntry;
  added: Set<GNode>;
  changed: Set<GNode>;
  removed: DiffRec[];
  /** Callers of changed code that didn't change themselves: what may need a look. */
  affected: GNode[];
}

export function marksFor(model: Model, entry: HistoryEntry): DiffMarks {
  const added = new Set<GNode>();
  const changed = new Set<GNode>();
  const d = entry.diff;
  if (d) {
    for (const r of d.added) { const n = model.byId.get(r.id); if (n) added.add(n); }
    for (const r of d.changed) { const n = model.byId.get(r.id); if (n) changed.add(n); }
  }
  const affected = new Set<GNode>();
  for (const n of [...changed, ...added]) {
    if (n.kind !== "Function" && n.kind !== "Class" && n.kind !== "ApiEndpoint") continue;
    for (const e of n.inE) {
      if (e.group !== "calls" && e.group !== "http" && e.group !== "runs") continue;
      if (!added.has(e.s) && !changed.has(e.s) && e.s.kind !== "TestingFile") affected.add(e.s);
    }
  }
  return { entry, added, changed, removed: d?.removed ?? [], affected: [...affected].slice(0, 40) };
}

const rtf = typeof Intl !== "undefined" && "RelativeTimeFormat" in Intl ? new Intl.RelativeTimeFormat(undefined, { numeric: "auto" }) : null;
export function ago(iso?: string): string {
  if (!iso) return "";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return "";
  const s = (t - Date.now()) / 1000;
  const units: [Intl.RelativeTimeFormatUnit, number][] = [["year", 31536000], ["month", 2592000], ["week", 604800],
    ["day", 86400], ["hour", 3600], ["minute", 60]];
  for (const [u, sec] of units) if (Math.abs(s) >= sec) return rtf ? rtf.format(Math.round(s / sec), u) : `${Math.round(-s / sec)} ${u}s ago`;
  return "just now";
}

/** "+2 ~3 −1" for the code in an entry (functions, classes, endpoints, agents, services…). */
export function tally(d: Diff | null): [number, number, number] {
  if (!d) return [0, 0, 0];
  const t: [number, number, number] = [0, 0, 0];
  for (const [label, c] of Object.entries(d.counts)) {
    if (label === "DataFile" || label === "TestingFile" || label === "DocumentationFile" || label === "TemplateMarkupFile") continue;
    t[0] += c[0]; t[1] += c[1]; t[2] += c[2];
  }
  return t;
}

export interface ChangesCallbacks {
  glyph(kind: Kind, n?: GNode): HTMLElement;
  pick(entry: HistoryEntry | null): void;
  openNode(n: GNode): void;
  openLink(url: string): void;
  close(): void;
  showInMap(): void;
}

const el = <K extends keyof HTMLElementTagNameMap>(tag: K, cls?: string, text?: string) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
};

function counts(t: [number, number, number]): HTMLElement {
  const wrap = el("span", "vc-counts");
  const [a, c, r] = t;
  if (a) wrap.append(el("span", "vc-add", `+${a}`));
  if (c) wrap.append(el("span", "vc-chg", `~${c}`));
  if (r) wrap.append(el("span", "vc-del", `−${r}`));
  return wrap;
}

export function renderChanges(host: HTMLElement, h: History, model: Model, selected: DiffMarks | null, cb: ChangesCallbacks): void {
  host.replaceChildren();
  const head = el("div", "panel-head");
  const close = el("button", "icon-btn", "Close");
  close.onclick = () => cb.close();
  head.append(close);
  const body = el("div", "panel-body");
  body.append(el("p", "panel-kind", "Version control"));
  const title = el("h2", "panel-title is-code", `${h.branch} @ ${h.short}`);
  body.append(title);
  const status: string[] = [];
  if (h.upstream) {
    status.push(h.ahead ? `${h.ahead} commit${h.ahead > 1 ? "s" : ""} not pushed to ${h.upstream}` : `in sync with ${h.upstream}`);
    if (h.behind) status.push(`${h.behind} behind`);
  } else status.push("no upstream branch");
  if (h.dirty) status.push("uncommitted changes");
  body.append(el("p", "panel-facts", status.join(" · ")));

  const list = el("ol", "vc-list");
  for (const e of h.entries) {
    const li = el("li", "vc-entry");
    const isSel = selected?.entry.id === e.id;
    const b = el("button", `vc-item${isSel ? " is-selected" : ""}${e.kind === "working" ? " is-working" : ""}`);
    b.setAttribute("aria-pressed", String(isSel));
    const top = el("span", "vc-top");
    if (e.kind === "working") top.append(el("span", "vc-dot is-working"), el("span", "vc-title", e.title));
    else top.append(el("span", `vc-dot${e.pushed === false && h.upstream ? " is-unpushed" : ""}`),
      el("span", "vc-sha", e.short ?? ""), el("span", "vc-title", e.title));
    top.append(counts(tally(e.diff)));
    const sub = el("span", "vc-sub");
    const bits: string[] = [];
    if (e.kind === "commit") {
      if (e.author) bits.push(e.author);
      if (e.date) bits.push(ago(e.date));
      if (h.upstream && e.pushed === false) bits.push("not pushed");
    }
    bits.push(`${e.files.length} file${e.files.length === 1 ? "" : "s"}`);
    sub.textContent = bits.join(" · ");
    b.append(top, sub);
    b.onclick = () => cb.pick(isSel ? null : e);
    li.append(b);
    if (isSel && selected) li.append(details(selected, model, cb));
    list.append(li);
  }
  body.append(list);
  host.append(head, body);
}

function details(m: DiffMarks, model: Model, cb: ChangesCallbacks): HTMLElement {
  const box = el("div", "vc-details");
  const e = m.entry;
  if (e.summary) box.append(el("p", "vc-summary", e.summary));
  const actions = el("div", "panel-actions");
  const map = el("button", "btn", "Show these in the map");
  map.onclick = () => cb.showInMap();
  actions.append(map);
  if (e.url) {
    const gh = el("button", "btn", "Open on GitHub");
    gh.onclick = () => cb.openLink(e.url!);
    actions.append(gh);
  }
  box.append(actions);
  if (!e.diff) {
    box.append(el("p", "panel-note", "This commit's graph couldn't be built, so only its files are listed."));
  }
  const section = (titleText: string, items: { label: string; kind: Kind; node?: GNode; where?: string; cls: string; note?: string }[]) => {
    if (!items.length) return;
    const sec = el("section", "rel");
    sec.append(el("h3", "rel-title", `${titleText} (${items.length})`));
    const ul = el("ul", "rel-list");
    for (const it of items.slice(0, 40)) {
      const li = el("li");
      const b = el("button", `rel-item ${it.cls}`);
      b.append(cb.glyph(it.kind, it.node), el("span", "rel-name", it.label));
      if (it.note) b.append(el("span", "vc-note", it.note));
      if (it.where) b.append(el("span", "rel-path", it.where));
      if (it.node) b.onclick = () => cb.openNode(it.node!);
      else b.disabled = true;
      li.append(b);
      ul.append(li);
    }
    if (items.length > 40) ul.append(el("li", "rel-more", `and ${items.length - 40} more`));
    sec.append(ul);
    box.append(sec);
  };
  const recs = (list: DiffRec[], cls: string) => list
    .filter((r) => !["DataFile", "TestingFile", "DocumentationFile", "TemplateMarkupFile"].includes(r.l))
    .map((r) => ({
      label: (r.q ?? r.n) + (r.l === "Function" ? "()" : ""), kind: r.l, node: model.byId.get(r.id), cls,
      where: r.p ? r.p.split("/").pop() + (r.line ? `:${r.line}` : "") : "", note: r.sig ? "signature" : undefined,
    }));
  if (e.diff) {
    section("Added", recs(e.diff.added, "is-added"));
    section("Changed", recs(e.diff.changed, "is-changed"));
    section("Removed", recs(e.diff.removed, "is-removed"));
  }
  section("May be affected", m.affected.map((n) => ({
    label: (n.props.qualname ?? n.name) + (n.kind === "Function" ? "()" : ""), kind: n.kind, node: n, cls: "",
    where: n.path.split("/").pop(), note: "calls changed code",
  })));
  section("Files", e.files.map((f) => ({
    label: f.path, kind: "DataFile" as Kind, cls: f.status === "added" ? "is-added" : f.status === "deleted" ? "is-removed" : "is-changed",
    node: model.byId.get(`SOURCECODEFILE:${f.path}:${f.path.split(".").pop()}`)
      ?? model.nodes.find((n) => n.path === f.path && (n.kind === "DataFile" || n.kind === "TestingFile" || n.kind === "DocumentationFile" || n.kind === "TemplateMarkupFile")),
  })));
  if (e.diff?.truncated) box.append(el("p", "panel-note", "Long lists are cut at 300 entries."));
  return box;
}
