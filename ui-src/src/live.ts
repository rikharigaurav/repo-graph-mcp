// The Live tab: changes to a watched folder as they happen (from `graph_repo.py live`), each one a set of
// graph changes, labelled with the Claude Code tool that made it when the plugin's hook reported it.
import type { GNode, Kind, Model, Payload } from "./model";
import { details, tally, type DiffMarks, type HistoryEntry } from "./changes";

export interface LiveEvent extends HistoryEntry {
  seq: number;
  at: string;
  source: "claude" | "disk" | "git";
  tools: string[];
  ms?: number;
  head?: string;
}
export interface LiveSince extends HistoryEntry { changes: number; by_claude: number; at: string }
export interface LiveState {
  root: string;
  name: string;
  started: string;
  seq: number;
  events: LiveEvent[]; // newest first
  since: LiveSince;
  hook_seen: string | null;
  building: boolean;
  error: string | null;
  interval: number;
}
export type Conn = "connecting" | "open" | "lost";

export interface LiveHandlers {
  hello(s: LiveState): void;
  change(ev: LiveEvent | null, since: LiveSince, seq: number): void;
  update(ev: LiveEvent, since: LiveSince): void;
  status(building: boolean, error: string | null): void;
  hook(seen: string): void;
  connection(c: Conn): void;
}

/** The stream from the live server. EventSource reconnects by itself; each reconnect starts with a hello. */
export function connectLive(h: LiveHandlers): { close(): void } {
  let failures = 0;
  h.connection("connecting");
  const es = new EventSource("api/events");
  es.onopen = () => { failures = 0; h.connection("open"); };
  es.onerror = () => {
    failures++;
    h.connection(failures > 2 ? "lost" : "connecting");
  };
  const on = (name: string, fn: (d: any) => void) => es.addEventListener(name, (m) => {
    try { fn(JSON.parse((m as MessageEvent).data)); } catch { /* ignore a bad message */ }
  });
  on("hello", (d) => h.hello(d as LiveState));
  on("change", (d) => h.change(d.event ?? null, d.since, d.seq));
  on("event", (d) => h.update(d.event, d.since));
  on("status", (d) => h.status(!!d.building, d.error ?? null));
  on("hook", (d) => h.hook(d.hook_seen));
  return { close: () => es.close() };
}

export async function fetchGraph(): Promise<Payload> {
  const r = await fetch("api/graph", { cache: "no-store" });
  if (!r.ok) throw new Error(`the live server answered ${r.status}`);
  return (await r.json()) as Payload;
}

const el = <K extends keyof HTMLElementTagNameMap>(tag: K, cls?: string, text?: string) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
};

/** "now", "12 s ago", "4 min ago", then the clock time. */
export function since(iso: string, now = Date.now()): string {
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return "";
  const s = Math.max(0, Math.round((now - t) / 1000));
  if (s < 5) return "now";
  if (s < 60) return `${s} s ago`;
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  return clock(iso);
}
export const clock = (iso: string) => new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });

/** What the overview banner says this change is ("Changes from Claude Code · Edit summary.py"). */
export function labelOf(e: LiveEvent): string {
  if (e.source === "claude") return `Claude Code · ${e.title}`;
  return e.title;
}

export interface LiveView {
  state: LiveState;
  conn: Conn;
  follow: boolean;
  selected: DiffMarks | null;
  fresh: Set<string>; // events that arrived since the last draw, for a brief highlight
}

export interface LiveCallbacks {
  glyph(kind: Kind, n?: GNode): HTMLElement;
  pick(entry: HistoryEntry | null): void;
  openNode(n: GNode): void;
  openLink(url: string): void;
  showInMap(): void;
  setFollow(on: boolean): void;
}

function counts(t: [number, number, number]): HTMLElement {
  const wrap = el("span", "vc-counts");
  const [a, c, r] = t;
  if (a) wrap.append(el("span", "vc-add", `+${a}`));
  if (c) wrap.append(el("span", "vc-chg", `~${c}`));
  if (r) wrap.append(el("span", "vc-del", `−${r}`));
  return wrap;
}

function source(e: LiveEvent): HTMLElement {
  const b = el("span", `live-src is-${e.source}`);
  if (e.source === "claude") b.textContent = e.tools.length ? `Claude Code · ${e.tools.join(", ")}` : "Claude Code";
  else if (e.source === "git") b.textContent = "Commit";
  else b.textContent = "File change";
  return b;
}

export function renderLive(host: HTMLElement, v: LiveView, model: Model, cb: LiveCallbacks): void {
  const keepScroll = host.scrollTop;
  host.replaceChildren();
  const st = v.state;
  const body = el("div", "panel-body live-body");
  const head = el("div", "live-head");
  const kind = el("p", "panel-kind", "Live changes");
  const status = el("span", `live-status is-${st.building ? "building" : v.conn}`);
  status.append(el("span", "live-pulse"), el("span", "live-status-text",
    v.conn === "lost" ? "Not connected" : v.conn === "connecting" ? "Connecting…" : st.building ? "Updating the graph…" : "Watching"));
  status.setAttribute("role", "status");
  head.append(kind, status);
  body.append(head);
  const title = el("h2", "panel-title is-code", st.name);
  title.title = st.root;
  body.append(title);
  const facts = el("p", "panel-facts live-facts");
  facts.append(`Watching since ${clock(st.started)} · checks every ${st.interval < 1 ? st.interval.toFixed(1) : st.interval} s`);
  if (st.hook_seen) {
    const hook = el("span", "live-hook", "Claude Code is reporting its edits");
    hook.title = `Last report ${clock(st.hook_seen)}`;
    facts.append(el("br"), hook);
  }
  body.append(facts);
  if (st.error) body.append(el("p", "panel-warn", `The last update failed: ${st.error}. Still watching.`));
  if (v.conn === "lost") {
    body.append(el("p", "panel-warn", "Lost the connection to the live server. It may have stopped: run the live command again, then reload this page."));
  }

  const follow = el("button", "live-follow");
  follow.type = "button";
  follow.setAttribute("aria-pressed", String(v.follow));
  follow.append(el("span", "live-follow-box"), el("span", "", "Show each new change as it arrives"));
  follow.onclick = () => cb.setFollow(!v.follow);
  body.append(follow);

  const sel = v.selected?.entry.id ?? null;
  const item = (e: HistoryEntry, top: HTMLElement[], sub: (string | Node)[], extra = ""): HTMLElement => {
    const li = el("li", "vc-entry");
    const isSel = sel === e.id;
    const b = el("button", `vc-item live-item${isSel ? " is-selected" : ""}${extra}`);
    b.type = "button";
    b.setAttribute("aria-pressed", String(isSel));
    const row = el("span", "vc-top");
    row.append(...top, counts(tally(e.diff)));
    const subEl = el("span", "vc-sub live-sub");
    subEl.append(...sub);
    b.append(row, subEl);
    b.onclick = () => cb.pick(isSel ? null : e);
    li.append(b);
    if (isSel && v.selected) li.append(details(v.selected, model, cb));
    return li;
  };

  // everything since watching began
  const list = el("ol", "vc-list live-list");
  const s = st.since;
  const sinceBits = [s.changes ? `${s.changes} change${s.changes > 1 ? "s" : ""}` : "no changes yet"];
  if (s.by_claude) sinceBits.push(`${s.by_claude} by Claude Code`);
  if (s.files.length) sinceBits.push(`${s.files.length} file${s.files.length > 1 ? "s" : ""}`);
  list.append(item(s, [el("span", "vc-dot is-since"), el("span", "vc-title", "Since you started watching")], [sinceBits.join(" · ")], " is-since"));
  body.append(list);

  const feed = el("ol", "vc-list live-list live-feed");
  feed.setAttribute("aria-label", "Changes, newest first");
  if (!st.events.length) {
    const empty = el("li", "live-empty");
    empty.append(
      el("p", "", "Waiting for changes. When Claude Code, or you, edit a file in this folder, it shows up here within a second or two and the graph updates in place."),
      el("p", "", "Edits made through Claude Code are labelled with the tool that made them once the repo-graph plugin's hook reports one."),
    );
    feed.append(empty);
  }
  for (const e of st.events) {
    const time = el("time", "live-time", since(e.at)); // kept current by tickTimes()
    time.dateTime = e.at;
    time.title = clock(e.at);
    const bits = [e.summary];
    if (e.ms !== undefined && e.ms > 0) bits.push(`updated in ${(e.ms / 1000).toFixed(e.ms < 1000 ? 2 : 1)} s`);
    const name = e.source === "git" ? e.title.replace(/^Committed /, "") : e.title;
    feed.append(item(e, [source(e), el("span", "vc-title", name)], [time, ` · ${bits.join(" · ")}`],
      v.fresh.has(e.id) ? " is-new" : ""));
  }
  body.append(feed);
  host.append(body);
  host.scrollTop = keepScroll;
}

/** Refresh the "12 s ago" labels without redrawing the list. */
export function tickTimes(host: HTMLElement): void {
  const now = Date.now();
  host.querySelectorAll<HTMLTimeElement>("time.live-time").forEach((t) => { t.textContent = since(t.dateTime, now); });
}
