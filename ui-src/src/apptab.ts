// The UI tab (live view only): the repo's own app running beside a review of what changed, where each
// changed file can be accepted or undone. The app is the repo's dev server in an iframe; its hot reload
// shows each edit as it lands. Undone changes are reported to Claude Code before the next prompt.
import { since, clock } from "./live";

export interface AppInfo { kind: "dev" | "static" | "custom" | "url"; dir: string; framework: string; cmd: string; pm?: string; needs_install: boolean }
export interface AppStatus {
  state: "idle" | "installing" | "starting" | "running" | "exited" | "failed";
  url: string | null;
  exit_code: number | null;
  error: string | null;
  app: AppInfo | null;
  log: string[];
}
export interface Pending {
  path: string;
  status: "added" | "modified" | "deleted";
  adds: number;
  dels: number;
  binary: boolean;
  source: "claude" | "disk" | "git";
  tools: string[];
  at: string | null;
}
export interface Undone { path: string; at: number; by: string; tools: string[]; was: string }
export interface ReviewState { pending: Pending[]; undone: Undone[] }
export interface Diff { path: string; binary: boolean; lines: string[]; truncated?: boolean }

export interface AppTabState {
  app: AppStatus | null;
  review: ReviewState;
  device: "desktop" | "tablet" | "phone";
  showLog: boolean;
  open: Set<string>; // files whose diff is expanded
  diffs: Map<string, Diff | "loading" | "error">;
  confirmAll: boolean;
  busy: Set<string>; // files with an action in flight ("*" for all)
}

export interface AppTabCallbacks {
  appAction(a: "start" | "stop" | "install"): void;
  review(a: "accept" | "undo" | "bring-back", paths: string[] | "all"): void;
  loadDiff(path: string): void;
  redraw(): void;
  openLink(url: string): void;
}

const el = <K extends keyof HTMLElementTagNameMap>(tag: K, cls?: string, text?: string) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
};
const button = (text: string, cls: string, onClick: () => void, title?: string) => {
  const b = el("button", cls, text);
  b.type = "button";
  b.onclick = onClick;
  if (title) b.title = title;
  return b;
};

/** The parts of the tab that must survive redraws (an iframe that's recreated reloads the app). */
export interface AppTabDom {
  root: HTMLElement;
  bar: HTMLElement;
  stage: HTMLElement;
  frame: HTMLIFrameElement;
  empty: HTMLElement;
  log: HTMLPreElement;
  review: HTMLElement;
}

export function mountAppTab(root: HTMLElement): AppTabDom {
  root.replaceChildren();
  const main = el("div", "app-main");
  const bar = el("div", "app-bar");
  const stage = el("div", "app-stage");
  const frame = el("iframe", "app-frame");
  frame.title = "Your app";
  frame.setAttribute("allow", "clipboard-read; clipboard-write; fullscreen");
  frame.hidden = true;
  const empty = el("div", "app-empty");
  stage.append(frame, empty);
  const log = el("pre", "app-log");
  log.hidden = true;
  log.setAttribute("aria-label", "App output");
  main.append(bar, stage, log);
  const review = el("aside", "review");
  review.setAttribute("aria-label", "Review changes");
  root.append(main, review);
  return { root, bar, stage, frame, empty, log, review };
}

const STATE_TEXT: Record<AppStatus["state"], string> = {
  idle: "Not running", installing: "Installing dependencies…", starting: "Starting…", running: "Running",
  exited: "Stopped", failed: "Failed",
};

export function renderAppTab(dom: AppTabDom, st: AppTabState, cb: AppTabCallbacks): void {
  renderBar(dom, st, cb);
  renderStage(dom, st, cb);
  renderReview(dom.review, st, cb);
}

function renderBar(dom: AppTabDom, st: AppTabState, cb: AppTabCallbacks): void {
  const a = st.app;
  const bar = dom.bar;
  bar.replaceChildren();
  const left = el("div", "app-bar-left");
  const state = a?.state ?? "idle";
  const pill = el("span", `app-pill is-${state}`);
  pill.append(el("span", "app-pill-dot"), el("span", "", a?.app ? STATE_TEXT[state] : "No app found"));
  pill.setAttribute("role", "status");
  left.append(pill);
  if (a?.app) {
    const what = el("span", "app-what");
    what.textContent = a.app.framework + (a.app.dir ? ` · ${a.app.dir}/` : "");
    if (a.app.cmd) what.title = a.app.cmd;
    left.append(what);
  }
  if (a?.url && state === "running") {
    const link = el("a", "app-url", a.url.replace(/^https?:\/\//, "").replace(/\/$/, ""));
    link.href = a.url;
    link.target = "_blank";
    link.rel = "noopener";
    left.append(link);
  }
  const right = el("div", "app-bar-right");
  if (state === "running" && a?.url) {
    const seg = el("div", "seg app-devices");
    seg.setAttribute("role", "group");
    seg.setAttribute("aria-label", "Screen size");
    for (const [d, label] of [["desktop", "Desktop"], ["tablet", "Tablet"], ["phone", "Phone"]] as const) {
      const b = button(label, "", () => { st.device = d; cb.redraw(); });
      b.setAttribute("aria-pressed", String(st.device === d));
      seg.append(b);
    }
    right.append(seg,
      button("Reload", "btn", () => { try { dom.frame.contentWindow?.location.reload(); } catch { dom.frame.src = dom.frame.src; } }),
      button("Open ↗", "btn", () => cb.openLink(a.url!), "Open the app in a new tab"));
  }
  const logBtn = button(st.showLog ? "Hide output" : "Output", "btn", () => { st.showLog = !st.showLog; cb.redraw(); });
  logBtn.setAttribute("aria-pressed", String(st.showLog));
  if (a?.app) right.append(logBtn);
  if (a?.app && a.app.kind !== "url") {
    if (state === "running" || state === "starting" || state === "installing") right.append(button("Stop", "btn", () => cb.appAction("stop")));
    else right.append(button(state === "idle" ? "Start the app" : "Start again", "btn btn-primary", () => cb.appAction("start")));
  }
  bar.append(left, right);
}

function renderStage(dom: AppTabDom, st: AppTabState, cb: AppTabCallbacks): void {
  const a = st.app;
  const running = !!(a && a.state === "running" && a.url);
  dom.stage.dataset.device = st.device;
  if (running) {
    if (dom.frame.dataset.url !== a!.url) {
      dom.frame.dataset.url = a!.url!;
      dom.frame.src = a!.url!;
    }
    dom.frame.hidden = false;
    dom.empty.hidden = true;
  } else {
    if (dom.frame.dataset.url) {
      dom.frame.removeAttribute("src");
      delete dom.frame.dataset.url;
    }
    dom.frame.hidden = true;
    dom.empty.hidden = false;
    dom.empty.replaceChildren(...emptyState(a, cb));
  }
  dom.log.hidden = !st.showLog;
  if (st.showLog) {
    const atBottom = dom.log.scrollTop + dom.log.clientHeight >= dom.log.scrollHeight - 8;
    dom.log.textContent = (a?.log ?? []).join("\n") || "No output yet.";
    if (atBottom) dom.log.scrollTop = dom.log.scrollHeight;
  }
}

function emptyState(a: AppStatus | null, cb: AppTabCallbacks): HTMLElement[] {
  const box = el("div", "app-empty-box");
  if (!a?.app) {
    box.append(
      el("h3", "", "No app to preview"),
      el("p", "", "No frontend was found in this folder (a package.json with a dev script for Vite, Next.js, React, Vue, Svelte, Astro or Angular, or an index.html)."),
      el("p", "", "To preview another kind of app, restart the live view with the command that runs it, for example:"),
      el("code", "app-cmd", 'live <folder> --ui-cmd "streamlit run app.py"'),
      el("p", "app-note", "You can still review changes on the right."),
    );
    return [box];
  }
  const info = a.app;
  const st = a.state;
  if (st === "installing" || st === "starting") {
    box.append(el("div", "app-spinner"), el("h3", "", st === "installing" ? "Installing dependencies…" : `Starting ${info.framework}…`),
      el("p", "", st === "installing" ? `Running ${info.pm ?? "npm"} install once in ${info.dir || "the repo"}/. The first time can take a minute.` : "Waiting for the dev server to print its address."));
    return [box];
  }
  box.append(el("h3", "", st === "failed" ? "The app stopped" : `Run ${info.framework} here`));
  if (st === "failed") box.append(el("p", "app-err", a.error ?? "It exited with an error. Open Output to see why."));
  else {
    box.append(el("p", "", "See your app here as Claude Code changes it. Edits show up as the page reloads, and each changed file can be accepted or undone on the right."));
    if (info.cmd) box.append(el("code", "app-cmd", (info.dir ? `cd ${info.dir} && ` : "") + info.cmd));
    if (info.needs_install) box.append(el("p", "app-note", `Its dependencies aren't installed yet: starting runs ${info.pm ?? "npm"} install first.`));
  }
  const row = el("div", "app-empty-actions");
  row.append(button(st === "failed" ? "Start again" : "Start the app", "btn btn-primary", () => cb.appAction("start")));
  if (st === "failed" && info.pm) row.append(button("Reinstall dependencies", "btn", () => cb.appAction("install")));
  box.append(row);
  return [box];
}

const SRC_TEXT = (p: Pending) => p.source === "claude" ? `Claude Code${p.tools.length ? " · " + p.tools.join(", ") : ""}`
  : p.source === "git" ? "Git" : "File change";

function renderReview(host: HTMLElement, st: AppTabState, cb: AppTabCallbacks): void {
  const keep = host.scrollTop;
  host.replaceChildren();
  const r = st.review;
  const head = el("div", "review-head");
  const title = el("h2", "review-title", "Review changes");
  head.append(title);
  const n = r.pending.length;
  if (n) {
    const actions = el("div", "review-all");
    if (st.confirmAll) {
      actions.append(el("span", "review-confirm", `Undo ${n} file${n > 1 ? "s" : ""}?`),
        button("Yes, undo", "btn btn-danger", () => { st.confirmAll = false; cb.review("undo", "all"); }),
        button("Cancel", "btn", () => { st.confirmAll = false; cb.redraw(); }));
    } else {
      actions.append(button("Accept all", "btn", () => cb.review("accept", "all")),
        button("Undo all", "btn", () => { st.confirmAll = true; cb.redraw(); setTimeout(() => { if (st.confirmAll) { st.confirmAll = false; cb.redraw(); } }, 6000); }));
    }
    head.append(actions);
  }
  host.append(head);
  const byClaude = r.pending.filter((p) => p.source === "claude").length;
  host.append(el("p", "review-sub", n
    ? `${n} changed file${n > 1 ? "s" : ""}${byClaude ? `, ${byClaude} by Claude Code` : ""}. Accept keeps a change; Undo puts the file back as it was.`
    : "Nothing to review. When Claude Code, or you, change a file here, it shows up in this list and in the app as it reloads."));

  const list = el("ul", "review-list");
  for (const p of r.pending) {
    const li = el("li", `review-item is-${p.status}`);
    const top = el("div", "review-row");
    const name = el("button", "review-file");
    name.type = "button";
    name.setAttribute("aria-expanded", String(st.open.has(p.path)));
    const dir = p.path.includes("/") ? p.path.slice(0, p.path.lastIndexOf("/") + 1) : "";
    name.append(el("span", "review-caret", st.open.has(p.path) ? "▾" : "▸"), el("span", "review-dir", dir),
      el("span", "review-base", p.path.slice(dir.length)));
    name.title = p.path;
    name.onclick = () => {
      if (st.open.has(p.path)) st.open.delete(p.path);
      else { st.open.add(p.path); if (!st.diffs.has(p.path)) cb.loadDiff(p.path); }
      cb.redraw();
    };
    top.append(name);
    const meta = el("div", "review-meta");
    meta.append(el("span", `live-src is-${p.source}`, SRC_TEXT(p)));
    if (p.status !== "modified") meta.append(el("span", `review-status is-${p.status}`, p.status === "added" ? "new file" : "deleted"));
    if (!p.binary && (p.adds || p.dels)) {
      const c = el("span", "vc-counts");
      if (p.adds) c.append(el("span", "vc-add", `+${p.adds}`));
      if (p.dels) c.append(el("span", "vc-del", `−${p.dels}`));
      meta.append(c);
    }
    if (p.at) { const t = el("time", "live-time", since(p.at)); t.dateTime = p.at; t.title = clock(p.at); meta.append(t); }
    const btns = el("div", "review-btns");
    const busy = st.busy.has(p.path) || st.busy.has("*");
    const acc = button("Accept", "btn btn-small", () => cb.review("accept", [p.path]), "Keep this change");
    const und = button("Undo", "btn btn-small", () => cb.review("undo", [p.path]),
      p.status === "added" ? "Delete this new file" : p.status === "deleted" ? "Restore the deleted file" : "Put the file back as it was");
    acc.disabled = und.disabled = busy;
    btns.append(acc, und);
    li.append(top, meta, btns);
    if (st.open.has(p.path)) li.append(diffView(st.diffs.get(p.path)));
    list.append(li);
  }
  host.append(list);

  if (r.undone.length) {
    const sec = el("section", "review-undone");
    sec.append(el("h3", "rel-title", `Undone (${r.undone.length})`));
    const ul = el("ul", "review-list");
    for (const u of r.undone.slice(0, 10)) {
      const li = el("li", "review-item is-undone");
      const row = el("div", "review-row");
      row.append(el("span", "review-base", u.path), button("Bring back", "btn btn-small", () => cb.review("bring-back", [u.path]),
        "Restore the version you undid"));
      li.append(row, el("div", "review-meta", `${u.by === "claude" ? "Claude Code's change" : "Change"} undone ${since(new Date(u.at * 1000).toISOString())}`));
      ul.append(li);
    }
    sec.append(ul, el("p", "review-sub", "Claude Code is told about these before your next message, so it won't redo them."));
    host.append(sec);
  }
  host.scrollTop = keep;
}

function diffView(d: Diff | "loading" | "error" | undefined): HTMLElement {
  const box = el("div", "review-diff");
  if (!d || d === "loading") { box.append(el("p", "review-sub", "Loading the diff…")); return box; }
  if (d === "error") { box.append(el("p", "app-err", "Couldn't load the diff.")); return box; }
  if (d.binary) { box.append(el("p", "review-sub", "Binary file: no line-by-line diff.")); return box; }
  if (!d.lines.length) { box.append(el("p", "review-sub", "No differences in the text.")); return box; }
  const pre = el("pre", "review-pre");
  for (const line of d.lines) {
    const cls = line.startsWith("@@") ? "d-hunk" : line.startsWith("+") ? "d-add" : line.startsWith("-") ? "d-del" : "d-ctx";
    pre.append(el("span", cls, line + "\n"));
  }
  box.append(pre);
  if (d.truncated) box.append(el("p", "review-sub", "Long diff: only the start is shown."));
  return box;
}
