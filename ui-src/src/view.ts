import {
  forceCollide, forceLink, forceManyBody, forceSimulation, forceX, forceY,
  type Simulation,
} from "d3-force";
import { cornerPath, flapPath, fontWithSize, halfWidth, iconPath, markFor } from "./icons";
import { KIND, type GNode, type Group, type Kind, type VLink } from "./model";

interface Palette {
  kind: Record<Kind, string>;
  edge: Record<Group, string>;
  ink: string; ink2: string; ink3: string; map: string; halo: string;
  regionFill: string; regionStroke: string; regionLabel: string; select: string; onKind: string;
}

const SHAPE_ORDER: Kind[] = ["Folder", "DocumentationFile", "TemplateMarkupFile", "TestingFile", "DataFile", "Class", "ApiEndpoint", "Function"];
const LABEL_RANK: Record<Kind, number> = {
  Folder: 1, ApiEndpoint: 2, DataFile: 3, TestingFile: 4, Class: 4, TemplateMarkupFile: 5, DocumentationFile: 6, Function: 6,
};

export interface ViewCallbacks {
  onSelect(n: GNode | null): void;
  onToggle(n: GNode): void; // double-click: expand file / collapse folder
  onHover(n: GNode | null, x: number, y: number): void;
}

export class GraphView {
  readonly canvas: HTMLCanvasElement;
  private ctx: CanvasRenderingContext2D;
  private sim: Simulation<GNode, VLink>;
  nodes: GNode[] = [];
  links: VLink[] = [];
  k = 1; tx = 0; ty = 0;
  private w = 0; private h = 0; private dpr = 1;
  selected: GNode | null = null;
  hovered: GNode | null = null;
  private lit = new Set<GNode>();
  private litLinks = new Set<VLink>();
  private pal!: Palette;
  private raf = 0;
  private dirty = true;
  private hullCache: Map<GNode, [number, number][]> = new Map();
  private hullFrame = 0;
  wheelZoom = false; // only zoom on plain wheel after the user has engaged with the canvas
  collapsed: Set<string> = new Set();
  /** Re-frame once the layout settles, unless the user has taken over the camera. */
  autoFit = false;
  reducedMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;

  constructor(private host: HTMLElement, private cb: ViewCallbacks) {
    this.canvas = document.createElement("canvas");
    this.canvas.tabIndex = 0;
    this.canvas.setAttribute("role", "img");
    this.canvas.setAttribute("aria-label", "Repository graph. Use the search box or the list in the details panel to move between nodes.");
    host.appendChild(this.canvas);
    this.ctx = this.canvas.getContext("2d")!;
    this.sim = forceSimulation<GNode, VLink>()
      .alphaDecay(0.035)
      .velocityDecay(0.42)
      .on("tick", () => this.invalidate(true))
      .on("end", () => { if (this.autoFit) { this.autoFit = false; this.fit(true); } });
    this.sim.stop();
    new ResizeObserver(() => this.resize()).observe(host);
    this.resize();
    this.readPalette();
    this.bindPointer();
    const loop = () => {
      this.raf = requestAnimationFrame(loop);
      if (this.dirty) { this.dirty = false; this.draw(); }
    };
    loop();
  }

  // ── data ────────────────────────────────────────────────────────────────
  setData(nodes: GNode[], links: VLink[], opts: { reseed: boolean }): void {
    const prev = new Set(this.nodes);
    this.nodes = nodes;
    this.links = links;
    if (opts.reseed) seedRadial(nodes);
    else {
      for (const n of nodes) {
        if (prev.has(n) && n.x !== undefined) continue;
        const anchor = nearestPlaced(n, prev);
        const a = Math.random() * Math.PI * 2;
        n.x = (anchor?.x ?? 0) + Math.cos(a) * 14;
        n.y = (anchor?.y ?? 0) + Math.sin(a) * 14;
        n.vx = n.vy = 0;
      }
    }
    const big = nodes.length > 1500;
    this.sim.nodes(nodes);
    this.sim
      .force("link", forceLink<GNode, VLink>(links)
        .id((d) => d.id)
        .distance((l) => linkDistance(l))
        .strength((l) => linkStrength(l)))
      .force("charge", forceManyBody<GNode>()
        .strength((d) => -(18 + d.r * (big ? 4 : 7)) * (d.kind === "Folder" ? 2.2 : 1))
        .distanceMax(big ? 260 : 420)
        .theta(big ? 1.1 : 0.9))
      .force("collide", forceCollide<GNode>((d) => hw(d) + 2.5).iterations(1))
      .force("territory", territoryForce(0.06))
      .force("x", forceX<GNode>(0).strength(0.035))
      .force("y", forceY<GNode>(0).strength(0.045));
    this.hullCache.clear();
    if (opts.reseed) {
      // most of the layout happens off-screen; the last stretch animates so the map visibly settles
      const warm = this.reducedMotion ? 320 : Math.min(160, Math.max(40, Math.round(40000 / Math.max(nodes.length, 1))));
      this.sim.alpha(1);
      for (let i = 0; i < warm; i++) this.sim.tick();
      this.fit(false);
      if (!this.reducedMotion) {
        this.autoFit = true;
        this.sim.alpha(0.25).restart();
      }
    } else {
      this.sim.alpha(this.reducedMotion ? 0 : 0.35).restart();
      if (this.reducedMotion) for (let i = 0; i < 120; i++) this.sim.tick();
    }
    this.refreshHighlight();
    this.invalidate(true);
  }

  invalidate(hulls = false): void {
    if (hulls) this.hullFrame++;
    this.dirty = true;
  }

  readPalette(): void {
    const cs = getComputedStyle(document.documentElement);
    const v = (name: string) => cs.getPropertyValue(name).trim();
    this.pal = {
      kind: Object.fromEntries(Object.values(KIND).map((k) => [k.key, v(k.color)])) as Record<Kind, string>,
      edge: {
        contains: v("--e-contains"), calls: v("--e-calls"), imports: v("--e-imports"),
        http: v("--e-http"), tests: v("--e-tests"), extends: v("--e-extends"),
      },
      ink: v("--ink"), ink2: v("--ink-2"), ink3: v("--ink-3"), map: v("--map"), halo: v("--halo"),
      regionFill: v("--region-fill"), regionStroke: v("--region-stroke"), regionLabel: v("--region-label"),
      select: v("--select"), onKind: v("--on-kind"),
    };
    this.invalidate();
  }

  // ── camera ──────────────────────────────────────────────────────────────
  resize(): void {
    const r = this.host.getBoundingClientRect();
    this.dpr = Math.min(window.devicePixelRatio || 1, 2);
    const first = this.w === 0;
    this.w = Math.max(1, r.width);
    this.h = Math.max(1, r.height);
    this.canvas.width = Math.round(this.w * this.dpr);
    this.canvas.height = Math.round(this.h * this.dpr);
    this.canvas.style.width = `${this.w}px`;
    this.canvas.style.height = `${this.h}px`;
    if (first) { this.tx = this.w / 2; this.ty = this.h / 2; }
    this.invalidate();
  }

  fit(animate = true, only?: GNode[]): void {
    const pts = only ?? this.nodes;
    if (!pts.length) return;
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (const n of pts) {
      x0 = Math.min(x0, n.x! - n.r); y0 = Math.min(y0, n.y! - n.r);
      x1 = Math.max(x1, n.x! + n.r); y1 = Math.max(y1, n.y! + n.r);
    }
    const pad = 36;
    const labelRoom = pts.length < 80 ? 150 : 0; // small graphs show labels to the right of each node
    const w = this.w - this.inset.right - labelRoom, h = this.h - this.inset.bottom;
    const k = Math.min(2.4, Math.max(0.05, Math.min((w - pad * 2) / (x1 - x0 || 1), (h - pad * 2) / (y1 - y0 || 1))));
    const target = { k, tx: w / 2 - ((x0 + x1) / 2) * k, ty: h / 2 - ((y0 + y1) / 2) * k };
    this.animateTo(target, animate);
  }

  /** Space covered by the details panel, so centring targets the part of the map still visible. */
  inset = { right: 0, bottom: 0 };

  centerOn(n: GNode, minK = 1.6): void {
    this.autoFit = false;
    const k = Math.max(this.k, minK);
    const cx = (this.w - this.inset.right) / 2, cy = (this.h - this.inset.bottom) / 2;
    this.animateTo({ k, tx: cx - n.x! * k, ty: cy - n.y! * k }, true);
  }

  zoomBy(factor: number, cx = this.w / 2, cy = this.h / 2): void {
    this.autoFit = false;
    const k = Math.min(8, Math.max(0.04, this.k * factor));
    const f = k / this.k;
    this.tx = cx - (cx - this.tx) * f;
    this.ty = cy - (cy - this.ty) * f;
    this.k = k;
    this.invalidate();
  }

  private animateTo(t: { k: number; tx: number; ty: number }, animate: boolean): void {
    if (!animate || this.reducedMotion) {
      this.k = t.k; this.tx = t.tx; this.ty = t.ty;
      this.invalidate();
      return;
    }
    const from = { k: this.k, tx: this.tx, ty: this.ty };
    const start = performance.now();
    const dur = 420;
    const step = (now: number) => {
      const p = Math.min(1, (now - start) / dur);
      const e = 1 - Math.pow(1 - p, 3);
      this.k = from.k + (t.k - from.k) * e;
      this.tx = from.tx + (t.tx - from.tx) * e;
      this.ty = from.ty + (t.ty - from.ty) * e;
      this.invalidate();
      if (p < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }

  private toWorld(sx: number, sy: number): [number, number] {
    return [(sx - this.tx) / this.k, (sy - this.ty) / this.k];
  }

  hit(sx: number, sy: number): GNode | null {
    const [x, y] = this.toWorld(sx, sy);
    let best: GNode | null = null;
    let bestD = Infinity;
    const slop = 5 / this.k;
    for (const n of this.nodes) {
      const dx = n.x! - x, dy = n.y! - y;
      const d = Math.sqrt(dx * dx + dy * dy) - hw(n);
      if (d < slop && d < bestD) { best = n; bestD = d; }
    }
    return best;
  }

  // ── selection & highlight ───────────────────────────────────────────────
  select(n: GNode | null): void {
    this.selected = n;
    this.refreshHighlight();
    this.invalidate();
  }

  private refreshHighlight(): void {
    this.lit.clear();
    this.litLinks.clear();
    const focus = this.selected ?? this.hovered;
    if (!focus) return;
    this.lit.add(focus);
    for (const l of this.links) {
      if (l.source === focus || l.target === focus) {
        this.litLinks.add(l);
        this.lit.add(l.source === focus ? l.target : l.source);
      }
    }
  }

  // ── pointer ─────────────────────────────────────────────────────────────
  private bindPointer(): void {
    const c = this.canvas;
    const pointers = new Map<number, { x: number; y: number }>();
    let drag: { node: GNode | null; sx: number; sy: number; moved: boolean; tx: number; ty: number } | null = null;
    let pinch: { d: number; k: number } | null = null;
    const pos = (e: PointerEvent | WheelEvent | MouseEvent) => {
      const r = c.getBoundingClientRect();
      return { x: e.clientX - r.left, y: e.clientY - r.top };
    };

    c.addEventListener("pointerdown", (e) => {
      c.focus({ preventScroll: true });
      this.wheelZoom = true;
      this.autoFit = false;
      const p = pos(e);
      pointers.set(e.pointerId, p);
      c.setPointerCapture(e.pointerId);
      if (pointers.size === 2) {
        const [a, b] = [...pointers.values()];
        pinch = { d: Math.hypot(a.x - b.x, a.y - b.y), k: this.k };
        drag = null;
        return;
      }
      const node = this.hit(p.x, p.y);
      drag = { node, sx: p.x, sy: p.y, moved: false, tx: this.tx, ty: this.ty };
      if (node) { node.fx = node.x; node.fy = node.y; }
    });
    c.addEventListener("pointermove", (e) => {
      const p = pos(e);
      if (pointers.has(e.pointerId)) pointers.set(e.pointerId, p);
      if (pinch && pointers.size === 2) {
        const [a, b] = [...pointers.values()];
        const d = Math.hypot(a.x - b.x, a.y - b.y);
        this.zoomBy((pinch.k * (d / pinch.d)) / this.k, (a.x + b.x) / 2, (a.y + b.y) / 2);
        return;
      }
      if (drag) {
        const dx = p.x - drag.sx, dy = p.y - drag.sy;
        if (!drag.moved && Math.hypot(dx, dy) > 4) {
          drag.moved = true;
          if (drag.node) this.sim.alphaTarget(0.12).restart();
        }
        if (drag.moved) {
          if (drag.node) {
            const [wx, wy] = this.toWorld(p.x, p.y);
            drag.node.fx = wx; drag.node.fy = wy;
          } else {
            this.tx = drag.tx + dx; this.ty = drag.ty + dy;
            this.invalidate();
          }
        }
        c.style.cursor = drag.node ? "grabbing" : "move";
        return;
      }
      const n = this.hit(p.x, p.y);
      if (n !== this.hovered) {
        this.hovered = n;
        if (!this.selected) this.refreshHighlight();
        this.invalidate();
      }
      c.style.cursor = n ? "pointer" : "grab";
      this.cb.onHover(n, p.x, p.y);
    });
    const end = (e: PointerEvent) => {
      pointers.delete(e.pointerId);
      if (pointers.size < 2) pinch = null;
      if (!drag) return;
      const d = drag;
      drag = null;
      if (d.node) {
        d.node.fx = null; d.node.fy = null;
        this.sim.alphaTarget(0);
      }
      if (!d.moved) {
        this.cb.onSelect(d.node);
      }
      c.style.cursor = "grab";
    };
    c.addEventListener("pointerup", end);
    c.addEventListener("pointercancel", end);
    c.addEventListener("pointerleave", () => {
      if (this.hovered) {
        this.hovered = null;
        if (!this.selected) this.refreshHighlight();
        this.invalidate();
      }
      this.cb.onHover(null, 0, 0);
    });
    c.addEventListener("dblclick", (e) => {
      const p = pos(e);
      const n = this.hit(p.x, p.y);
      if (n) this.cb.onToggle(n);
      else this.zoomBy(1.6, p.x, p.y);
    });
    c.addEventListener("wheel", (e) => {
      if (!this.wheelZoom && !e.ctrlKey && !e.metaKey) return; // let the chat scroll
      e.preventDefault();
      const p = pos(e);
      const delta = e.deltaMode === 1 ? e.deltaY * 16 : e.deltaY;
      this.zoomBy(Math.exp(-delta * (e.ctrlKey ? 0.01 : 0.0018)), p.x, p.y);
    }, { passive: false });
    c.addEventListener("keydown", (e) => {
      const step = 60;
      if (e.key === "+" || e.key === "=") this.zoomBy(1.25);
      else if (e.key === "-" || e.key === "_") this.zoomBy(0.8);
      else if (e.key === "0") this.fit();
      else if (e.key === "ArrowLeft") { this.tx += step; this.invalidate(); }
      else if (e.key === "ArrowRight") { this.tx -= step; this.invalidate(); }
      else if (e.key === "ArrowUp") { this.ty += step; this.invalidate(); }
      else if (e.key === "ArrowDown") { this.ty -= step; this.invalidate(); }
      else return;
      e.preventDefault();
    });
  }

  // ── drawing ─────────────────────────────────────────────────────────────
  private draw(): void {
    const { ctx, pal, k } = this;
    ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
    ctx.clearRect(0, 0, this.w, this.h);
    ctx.save();
    ctx.translate(this.tx, this.ty);
    ctx.scale(k, k);
    const focus = this.selected ?? this.hovered;
    const dim = !!focus;

    this.drawRegions(dim);

    // links, batched by style
    const groups: Group[] = ["contains", "imports", "extends", "tests", "calls", "http"];
    for (const g of groups) {
      const ls = this.links.filter((l) => l.group === g && !this.litLinks.has(l));
      if (!ls.length) continue;
      this.strokeLinks(ls, g, dim ? 0.08 : baseAlpha(g), false);
    }
    if (this.litLinks.size) {
      for (const g of groups) {
        const ls = [...this.litLinks].filter((l) => l.group === g);
        if (ls.length) this.strokeLinks(ls, g, 0.95, true);
      }
    }

    // nodes, batched by kind
    for (const kind of SHAPE_ORDER) {
      const ns = this.nodes.filter((n) => n.kind === kind);
      if (!ns.length) continue;
      const faded = dim ? ns.filter((n) => !this.lit.has(n)) : ns;
      const strong = dim ? ns.filter((n) => this.lit.has(n)) : [];
      if (faded.length) this.fillShapes(faded, kind, dim ? 0.22 : 1);
      if (strong.length) this.fillShapes(strong, kind, 1);
    }
    if (this.selected) {
      const n = this.selected;
      ctx.beginPath();
      ctx.arc(n.x!, n.y!, Math.max(0.5, hw(n) * 1.08 + 4 / k), 0, Math.PI * 2);
      ctx.strokeStyle = pal.select;
      ctx.lineWidth = 2 / k;
      ctx.stroke();
    }
    ctx.restore();
    this.drawLabels(dim);
  }

  private strokeLinks(ls: VLink[], g: Group, alpha: number, lit: boolean): void {
    const { ctx, k } = this;
    ctx.globalAlpha = alpha;
    ctx.strokeStyle = this.pal.edge[g];
    const px = 1 / k;
    ctx.lineWidth = (g === "http" ? 1.8 : g === "contains" ? 0.8 : lit ? 1.4 : 1) * px * (lit ? 1.3 : 1);
    ctx.setLineDash(g === "imports" ? [4 * px, 3 * px] : g === "tests" ? [1.2 * px, 2.6 * px] : []);
    ctx.beginPath();
    for (const l of ls) {
      const s = l.source, t = l.target;
      ctx.moveTo(s.x!, s.y!);
      if (g === "contains") ctx.lineTo(t.x!, t.y!);
      else {
        const [cx, cy] = bend(s, t);
        ctx.quadraticCurveTo(cx, cy, t.x!, t.y!);
      }
    }
    ctx.stroke();
    ctx.setLineDash([]);
    // arrowheads where direction matters and they are legible
    if (g !== "contains" && (lit || k > 1.2) && ls.length < 1500) {
      ctx.fillStyle = this.pal.edge[g];
      ctx.beginPath();
      for (const l of ls) arrowHead(ctx, l, k);
      ctx.fill();
    }
    ctx.globalAlpha = 1;
  }

  private fillShapes(ns: GNode[], kind: Kind, alpha: number): void {
    const { ctx, k, pal } = this;
    const color = pal.kind[kind];
    const shape = KIND[kind].shape;
    ctx.globalAlpha = alpha;
    const external = kind === "ApiEndpoint" ? ns.filter((n) => n.props.defined_in_repo === false) : [];
    const solid = external.length ? ns.filter((n) => n.props.defined_in_repo !== false) : ns;
    ctx.fillStyle = color;
    ctx.beginPath();
    for (const n of solid) iconPath(ctx, shape, n.x!, n.y!, n.r);
    ctx.fill();
    if ((shape === "file" || shape === "folder") && k * 5 > 3) {
      // a lighter turned corner on pages, a lighter front flap on folders
      ctx.globalAlpha = alpha * (shape === "file" ? 0.45 : 0.2);
      ctx.fillStyle = pal.onKind;
      ctx.beginPath();
      for (const n of solid) (shape === "file" ? cornerPath : flapPath)(ctx, n.x!, n.y!, n.r);
      ctx.fill();
      ctx.globalAlpha = alpha;
    }
    if (external.length) {
      ctx.fillStyle = pal.map;
      ctx.strokeStyle = color;
      ctx.lineWidth = 1.4 / k;
      ctx.setLineDash([2 / k, 1.6 / k]);
      ctx.beginPath();
      for (const n of external) iconPath(ctx, shape, n.x!, n.y!, n.r);
      ctx.fill();
      ctx.stroke();
      ctx.setLineDash([]);
    }
    // marks inside the icons (extension, ƒ, C, +) once they're big enough to read
    ctx.fillStyle = pal.onKind;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    const mono = getMono(), sans = getSans();
    let lastFont = "";
    for (const n of solid) {
      const m = markFor(n, shape, this.collapsed.has(n.id), mono, sans);
      if (!m || m.size * k < 6.5) continue;
      const font = fontWithSize(m, m.size);
      if (font !== lastFont) { ctx.font = font; lastFont = font; }
      ctx.fillText(m.text, n.x!, n.y! + m.dy);
    }
    ctx.globalAlpha = 1;
  }

  private drawRegions(dim: boolean): void {
    const { ctx, k, pal } = this;
    const groups = new Map<GNode, GNode[]>();
    const subs = new Map<GNode, GNode[]>();
    for (const n of this.nodes) {
      if (n.region) (groups.get(n.region) ?? groups.set(n.region, []).get(n.region)!).push(n);
      if (k > 1.15 && n.subregion) (subs.get(n.subregion) ?? subs.set(n.subregion, []).get(n.subregion)!).push(n);
    }
    // a lone top-level folder would just outline the whole map; only draw territories when there are several
    if (groups.size < 2 && subs.size === 0) { this.regionLabels = []; return; }
    const recompute = this.hullFrame % 3 === 0 || this.hullCache.size === 0;
    this.regionLabels = [];
    const paint = (owner: GNode, members: GNode[], nested: boolean) => {
      if (members.length < 2 && !nested) return;
      let hull = this.hullCache.get(owner);
      if (!hull || recompute) {
        hull = paddedHull(members, nested ? 9 : 16);
        this.hullCache.set(owner, hull);
      }
      if (hull.length < 3) return;
      ctx.beginPath();
      smoothClosed(ctx, hull);
      if (nested) {
        ctx.setLineDash([3 / k, 3 / k]);
        ctx.strokeStyle = pal.regionStroke;
        ctx.lineWidth = 1 / k;
        ctx.globalAlpha = dim ? 0.4 : 0.9;
        ctx.stroke();
        ctx.setLineDash([]);
      } else {
        ctx.fillStyle = pal.regionFill;
        ctx.globalAlpha = dim ? 0.55 : 1;
        ctx.fill();
        ctx.strokeStyle = pal.regionStroke;
        ctx.lineWidth = 1 / k;
        ctx.stroke();
      }
      ctx.globalAlpha = 1;
      let top = hull[0];
      for (const p of hull) if (p[1] < top[1]) top = p;
      let cx = 0;
      for (const p of hull) cx += p[0];
      cx /= hull.length;
      this.regionLabels.push({ name: owner.name, x: cx, y: top[1], nested, size: members.length });
    };
    for (const [owner, members] of groups) paint(owner, members, false);
    for (const [owner, members] of subs) if (members.length >= 3) paint(owner, members, true);
  }

  private regionLabels: { name: string; x: number; y: number; nested: boolean; size: number }[] = [];

  private drawLabels(dim: boolean): void {
    const { ctx, k, pal } = this;
    ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
    const font = getComputedStyle(document.documentElement).getPropertyValue("--font").trim() || "system-ui";
    const occupied: [number, number, number, number][] = [];
    const free = (x0: number, y0: number, x1: number, y1: number) => {
      for (const b of occupied) if (x0 < b[2] && x1 > b[0] && y0 < b[3] && y1 > b[1]) return false;
      occupied.push([x0, y0, x1, y1]);
      return true;
    };
    ctx.textBaseline = "middle";
    ctx.lineJoin = "round";

    // folder territories read like place names on a map
    for (const r of this.regionLabels.sort((a, b) => b.size - a.size)) {
      const size = r.nested ? 11.5 : Math.min(17, 12 + Math.log2(1 + r.size) * 0.7);
      ctx.font = `italic ${r.nested ? 500 : 560} ${size}px ${font}`;
      const sx = r.x * k + this.tx, sy = r.y * k + this.ty - size * 0.55;
      const w = ctx.measureText(r.name).width;
      if (!free(sx - w / 2 - 3, sy - size / 2, sx + w / 2 + 3, sy + size / 2)) continue;
      ctx.textAlign = "center";
      ctx.globalAlpha = dim ? 0.5 : 1;
      ctx.strokeStyle = pal.halo;
      ctx.lineWidth = 3;
      ctx.strokeText(r.name, sx, sy);
      ctx.fillStyle = pal.regionLabel;
      ctx.fillText(r.name, sx, sy);
    }
    ctx.globalAlpha = 1;
    ctx.textAlign = "left";

    // node labels: priority order, greedy collision avoidance, level of detail by zoom
    const cand: { n: GNode; pri: number }[] = [];
    for (const n of this.nodes) {
      const sr = n.r * k;
      const isLit = this.lit.has(n);
      let show = isLit;
      if (!show && !dim) {
        if (n.kind === "Folder") show = (k > 0.45 && n.depth > (this.regionLabels.length ? 1 : 0)) || k > 1.2;
        else if (n.kind === "ApiEndpoint") show = k > 0.5;
        else if (n.kind === "Function") show = k > 1.9 || (k > 1.1 && n.deg > 4);
        else show = k > 1.05 || (k > 0.6 && sr > 5);
      }
      if (!show) continue;
      const pri = (n === this.selected ? -10 : n === this.hovered ? -9 : isLit ? -5 : 0) + LABEL_RANK[n.kind] - Math.min(3, n.deg / 10);
      cand.push({ n, pri });
    }
    cand.sort((a, b) => a.pri - b.pri);
    let drawn = 0;
    for (const { n } of cand) {
      if (drawn > 260) break;
      const strong = n === this.selected || n === this.hovered;
      const size = strong ? 12.5 : n.kind === "Folder" ? 11.5 : 11;
      const isCode = n.kind === "Function" || n.kind === "Class" || n.kind === "ApiEndpoint";
      ctx.font = `${strong || n.kind === "Folder" ? 600 : 450} ${size}px ${isCode ? getMono() : font}`;
      const label = labelFor(n);
      const sx = n.x! * k + this.tx + hw(n) * k + 5;
      const sy = n.y! * k + this.ty;
      if (sx > this.w + 10 || sy < -10 || sy > this.h + 10 || sx < -200) continue;
      const w = ctx.measureText(label).width;
      if (!free(sx - 2, sy - size / 2 - 1, sx + w + 2, sy + size / 2 + 1)) continue;
      ctx.strokeStyle = pal.halo;
      ctx.lineWidth = 3.2;
      ctx.strokeText(label, sx, sy);
      ctx.fillStyle = strong ? pal.ink : n.kind === "Function" ? pal.ink2 : pal.ink;
      ctx.fillText(label, sx, sy);
      drawn++;
    }
  }
}

const hw = (n: GNode) => halfWidth(KIND[n.kind].shape, n.r);

let sansCache = "";
function getSans(): string {
  if (!sansCache) sansCache = getComputedStyle(document.documentElement).getPropertyValue("--font").trim() || "system-ui";
  return sansCache;
}

let monoCache = "";
function getMono(): string {
  if (!monoCache) monoCache = getComputedStyle(document.documentElement).getPropertyValue("--mono").trim() || "monospace";
  return monoCache;
}

function labelFor(n: GNode): string {
  if (n.kind === "Function") {
    const q: string = n.props.qualname ?? n.name;
    return (q.includes(".") ? q.split(".").slice(-2).join(".") : q) + "()";
  }
  if (n.kind === "Folder") return n.props.is_root ? n.name : n.name + "/";
  return n.name;
}

function baseAlpha(g: Group): number {
  return { contains: 0.32, calls: 0.42, imports: 0.24, http: 0.85, tests: 0.5, extends: 0.6 }[g];
}

function linkDistance(l: VLink): number {
  if (l.group === "contains") {
    const s = l.source.kind;
    if (s === "Function" || s === "ApiEndpoint") return 12 + l.target.r;
    if (s === "Class") return 18 + l.target.r;
    if (s === "Folder") return 46 + l.source.r + l.target.r;
    return 24 + l.target.r;
  }
  return l.group === "http" ? 140 : 90;
}

function linkStrength(l: VLink): number {
  switch (l.group) {
    case "contains": return 0.85;
    case "http": return 0.035;
    case "calls": return l.lifted ? 0.01 : 0.025;
    case "extends": return 0.05;
    default: return 0.01;
  }
}

function bend(s: GNode, t: GNode): [number, number] {
  const mx = (s.x! + t.x!) / 2, my = (s.y! + t.y!) / 2;
  const dx = t.x! - s.x!, dy = t.y! - s.y!;
  return [mx - dy * 0.14, my + dx * 0.14];
}

function arrowHead(ctx: CanvasRenderingContext2D, l: VLink, k: number): void {
  const s = l.source, t = l.target;
  const [cx, cy] = bend(s, t);
  const ang = Math.atan2(t.y! - cy, t.x! - cx);
  const tipX = t.x! - Math.cos(ang) * (hw(t) * 0.92 + 1.5 / k);
  const tipY = t.y! - Math.sin(ang) * (hw(t) * 0.92 + 1.5 / k);
  const len = 6 / k, wid = 3 / k;
  ctx.moveTo(tipX, tipY);
  ctx.lineTo(tipX - Math.cos(ang) * len - Math.sin(ang) * wid, tipY - Math.sin(ang) * len + Math.cos(ang) * wid);
  ctx.lineTo(tipX - Math.cos(ang) * len + Math.sin(ang) * wid, tipY - Math.sin(ang) * len - Math.cos(ang) * wid);
  ctx.closePath();
}

/** Pull each node toward the centroid of its top-level folder so territories stay compact and
 *  cross-cutting links (HTTP, calls) don't drag one folder's nodes into another's region. */
function territoryForce(strength: number) {
  let nodes: GNode[] = [];
  const force = (alpha: number) => {
    const sum = new Map<GNode, [number, number, number]>();
    for (const n of nodes) {
      if (!n.region) continue;
      const s = sum.get(n.region) ?? [0, 0, 0];
      s[0] += n.x!; s[1] += n.y!; s[2] += 1;
      sum.set(n.region, s);
    }
    const k = strength * alpha;
    for (const n of nodes) {
      if (!n.region) continue;
      const s = sum.get(n.region)!;
      if (s[2] < 2) continue;
      n.vx! += (s[0] / s[2] - n.x!) * k;
      n.vy! += (s[1] / s[2] - n.y!) * k;
    }
  };
  force.initialize = (ns: GNode[]) => { nodes = ns; };
  return force;
}

// ── geometry helpers ─────────────────────────────────────────────────────────

/** Radial-tree seed: every subtree gets an angular wedge proportional to its size. */
function seedRadial(nodes: GNode[]): void {
  const set = new Set(nodes);
  const kids = new Map<GNode, GNode[]>();
  const roots: GNode[] = [];
  for (const n of nodes) {
    let p = n.parent;
    while (p && !set.has(p)) p = p.parent;
    if (p) (kids.get(p) ?? kids.set(p, []).get(p)!).push(n);
    else roots.push(n);
  }
  const size = new Map<GNode, number>();
  const measure = (n: GNode): number => {
    let s = 1;
    for (const c of kids.get(n) ?? []) s += measure(c);
    size.set(n, s);
    return s;
  };
  let total = 0;
  for (const r of roots) total += measure(r);
  const place = (n: GNode, a0: number, a1: number, depth: number) => {
    const a = (a0 + a1) / 2;
    const rad = depth === 0 ? 0 : 40 + depth * 62 + Math.sqrt(size.get(n)!) * 2;
    n.x = Math.cos(a) * rad + (Math.random() - 0.5) * 4;
    n.y = Math.sin(a) * rad + (Math.random() - 0.5) * 4;
    n.vx = n.vy = 0;
    const children = (kids.get(n) ?? []).sort((x, y) => size.get(y)! - size.get(x)!);
    const span = a1 - a0;
    let cur = a0;
    const sum = children.reduce((s, c) => s + size.get(c)!, 0) || 1;
    for (const c of children) {
      const w = (span * size.get(c)!) / sum;
      place(c, cur, cur + w, depth + 1);
      cur += w;
    }
  };
  let cur = 0;
  for (const r of roots) {
    const w = (Math.PI * 2 * size.get(r)!) / (total || 1);
    place(r, cur, cur + w, roots.length === 1 ? 0 : 1);
    cur += w;
  }
}

function nearestPlaced(n: GNode, placed: Set<GNode>): GNode | null {
  for (let a = n.parent; a; a = a.parent) if (placed.has(a) && a.x !== undefined) return a;
  for (const e of [...n.inE, ...n.outE]) {
    const o = e.s === n ? e.t : e.s;
    if (placed.has(o) && o.x !== undefined) return o;
  }
  return null;
}

function paddedHull(members: GNode[], pad: number): [number, number][] {
  const pts: [number, number][] = [];
  for (const n of members) {
    const r = n.r + pad;
    for (let i = 0; i < 8; i++) {
      const a = (i * Math.PI) / 4;
      pts.push([n.x! + Math.cos(a) * r, n.y! + Math.sin(a) * r]);
    }
  }
  return convexHull(pts);
}

function convexHull(points: [number, number][]): [number, number][] {
  if (points.length < 3) return points;
  const p = points.slice().sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  const cross = (o: number[], a: number[], b: number[]) => (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]);
  const lower: [number, number][] = [];
  for (const pt of p) {
    while (lower.length >= 2 && cross(lower[lower.length - 2], lower[lower.length - 1], pt) <= 0) lower.pop();
    lower.push(pt);
  }
  const upper: [number, number][] = [];
  for (let i = p.length - 1; i >= 0; i--) {
    const pt = p[i];
    while (upper.length >= 2 && cross(upper[upper.length - 2], upper[upper.length - 1], pt) <= 0) upper.pop();
    upper.push(pt);
  }
  upper.pop();
  lower.pop();
  return lower.concat(upper);
}

function smoothClosed(ctx: CanvasRenderingContext2D, pts: [number, number][]): void {
  const n = pts.length;
  const mid = (i: number): [number, number] => {
    const a = pts[i % n], b = pts[(i + 1) % n];
    return [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2];
  };
  const m0 = mid(0);
  ctx.moveTo(m0[0], m0[1]);
  for (let i = 1; i <= n; i++) {
    const p = pts[i % n];
    const m = mid(i);
    ctx.quadraticCurveTo(p[0], p[1], m[0], m[1]);
  }
  ctx.closePath();
}
