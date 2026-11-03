// Node icons, shared by the map (canvas, world units) and the legend/panel glyphs.
//   folder   → folder with a tab; a folded folder shows "+"
//   file     → page with a turned corner; the extension is printed inside when it's legible
//   dot      → function: round badge with "ƒ"
//   diamond  → class: diamond with "C"
//   hex      → API endpoint
//   spark    → the GenAI layer (four-point sparkle)
//   agent    → AI agent: a robot head with an antenna and two eyes
//   step     → agent step: an arrow tag with its order number
//   server   → MCP server: a rack unit with two bays
//   cloud    → external API / SaaS service; db → a cylinder for databases, vector and graph stores, caches
//   tag      → environment variable (a key tag)
import type { GNode, Shape } from "./model";

type Ctx = CanvasRenderingContext2D;

/** Half the drawn width, in units of r: used for label offsets, hit tests, collisions and arrow tips. */
export function halfWidth(shape: Shape, r: number): number {
  switch (shape) {
    case "folder": return r * 1.25;
    case "file": return r * 0.85;
    case "diamond": return r * 1.15;
    case "hex": return r * 1.1;
    case "spark": return r * 1.25;
    case "agent": return r * 1.05;
    case "step": return r * 1.2;
    case "server": return r * 0.95;
    case "cloud": return r * 1.3;
    case "db": return r * 0.95;
    case "tag": return r * 1.25;
    default: return r;
  }
}

function roundRect(ctx: Ctx | Path2D, L: number, T: number, w: number, h: number, c: number): void {
  const R = L + w, B = T + h;
  ctx.moveTo(L + c, T);
  ctx.lineTo(R - c, T);
  ctx.arcTo(R, T, R, T + c, c);
  ctx.lineTo(R, B - c);
  ctx.arcTo(R, B, R - c, B, c);
  ctx.lineTo(L + c, B);
  ctx.arcTo(L, B, L, B - c, c);
  ctx.lineTo(L, T + c);
  ctx.arcTo(L, T, L + c, T, c);
  ctx.closePath();
}

export function iconPath(ctx: Ctx | Path2D, shape: Shape, x: number, y: number, r: number): void {
  r = Math.max(0.5, r);
  switch (shape) {
    case "folder": {
      const w = r * 2.5, h = r * 1.9, c = r * 0.22;
      const L = x - w / 2, R = x + w / 2, T = y - h / 2, B = y + h / 2;
      const th = h * 0.2, tw = w * 0.4;
      ctx.moveTo(L + c, T);
      ctx.lineTo(L + tw - th * 0.4, T);
      ctx.lineTo(L + tw + th * 0.5, T + th);
      ctx.lineTo(R - c, T + th);
      ctx.arcTo(R, T + th, R, T + th + c, c);
      ctx.lineTo(R, B - c);
      ctx.arcTo(R, B, R - c, B, c);
      ctx.lineTo(L + c, B);
      ctx.arcTo(L, B, L, B - c, c);
      ctx.lineTo(L, T + c);
      ctx.arcTo(L, T, L + c, T, c);
      ctx.closePath();
      break;
    }
    case "file": {
      const w = r * 1.7, h = r * 2.15, f = w * 0.38, c = r * 0.18;
      const L = x - w / 2, R = x + w / 2, T = y - h / 2, B = y + h / 2;
      ctx.moveTo(L + c, T);
      ctx.lineTo(R - f, T);
      ctx.lineTo(R, T + f);
      ctx.lineTo(R, B - c);
      ctx.arcTo(R, B, R - c, B, c);
      ctx.lineTo(L + c, B);
      ctx.arcTo(L, B, L, B - c, c);
      ctx.lineTo(L, T + c);
      ctx.arcTo(L, T, L + c, T, c);
      ctx.closePath();
      break;
    }
    case "diamond":
      ctx.moveTo(x, y - r * 1.15);
      ctx.lineTo(x + r * 1.15, y);
      ctx.lineTo(x, y + r * 1.15);
      ctx.lineTo(x - r * 1.15, y);
      ctx.closePath();
      break;
    case "hex":
      for (let i = 0; i < 6; i++) {
        const a = Math.PI / 6 + (i * Math.PI) / 3;
        const px = x + Math.cos(a) * r * 1.1, py = y + Math.sin(a) * r * 1.1;
        if (i === 0) ctx.moveTo(px, py);
        else ctx.lineTo(px, py);
      }
      ctx.closePath();
      break;
    case "spark": {
      const o = r * 1.3, i = r * 0.42;
      for (let k = 0; k < 8; k++) {
        const a = -Math.PI / 2 + (k * Math.PI) / 4;
        const rad = k % 2 === 0 ? o : i;
        const px = x + Math.cos(a) * rad, py = y + Math.sin(a) * rad;
        if (k === 0) ctx.moveTo(px, py);
        else ctx.lineTo(px, py);
      }
      ctx.closePath();
      break;
    }
    case "agent": {
      const w = r * 2.05, h = r * 1.6, T = y - h / 2 + r * 0.25;
      roundRect(ctx, x - w / 2, T, w, h, r * 0.42);
      // antenna: stem + ball
      ctx.moveTo(x + r * 0.09, T);
      ctx.lineTo(x + r * 0.09, T - r * 0.32);
      ctx.lineTo(x - r * 0.09, T - r * 0.32);
      ctx.lineTo(x - r * 0.09, T);
      ctx.closePath();
      ctx.moveTo(x + r * 0.22, T - r * 0.45);
      ctx.arc(x, T - r * 0.45, r * 0.22, 0, Math.PI * 2);
      break;
    }
    case "step": {
      const w = r * 2.4, h = r * 1.6, L = x - w / 2, R = x + w / 2, T = y - h / 2, B = y + h / 2, tip = r * 0.62;
      ctx.moveTo(L, T);
      ctx.lineTo(R - tip, T);
      ctx.lineTo(R, y);
      ctx.lineTo(R - tip, B);
      ctx.lineTo(L, B);
      ctx.lineTo(L + tip * 0.55, y);
      ctx.closePath();
      break;
    }
    case "server":
      roundRect(ctx, x - r * 0.95, y - r * 1.1, r * 1.9, r * 2.2, r * 0.28);
      break;
    case "cloud":
      ctx.moveTo(x - r * 0.55 + r * 0.62, y + r * 0.2);
      ctx.arc(x - r * 0.55, y + r * 0.2, r * 0.62, 0, Math.PI * 2);
      ctx.moveTo(x + r * 0.12 + r * 0.8, y - r * 0.18);
      ctx.arc(x + r * 0.12, y - r * 0.18, r * 0.8, 0, Math.PI * 2);
      ctx.moveTo(x + r * 0.72 + r * 0.56, y + r * 0.26);
      ctx.arc(x + r * 0.72, y + r * 0.26, r * 0.56, 0, Math.PI * 2);
      roundRect(ctx, x - r * 1.15, y + r * 0.2, r * 2.4, r * 0.62, r * 0.3);
      break;
    case "db": {
      const w = r * 1.9, h = r * 2.2, e = r * 0.36, L = x - w / 2, R = x + w / 2, T = y - h / 2 + e, B = y + h / 2 - e;
      ctx.moveTo(L, T);
      ctx.lineTo(L, B);
      ctx.ellipse(x, B, w / 2, e, 0, Math.PI, 0, true);
      ctx.lineTo(R, T);
      ctx.ellipse(x, T, w / 2, e, 0, 0, Math.PI, true);
      ctx.closePath();
      break;
    }
    case "tag": {
      const w = r * 2.5, h = r * 1.5, L = x - w / 2, R = x + w / 2, T = y - h / 2, B = y + h / 2, c = r * 0.25, n = r * 0.62;
      ctx.moveTo(L, y);
      ctx.lineTo(L + n, T);
      ctx.lineTo(R - c, T);
      ctx.arcTo(R, T, R, T + c, c);
      ctx.lineTo(R, B - c);
      ctx.arcTo(R, B, R - c, B, c);
      ctx.lineTo(L + n, B);
      ctx.closePath();
      break;
    }
    default:
      ctx.moveTo(x + r, y);
      ctx.arc(x, y, r, 0, Math.PI * 2);
  }
}

/** The front flap of a folder, drawn lighter over the fill so the tab reads as the back panel. */
export function flapPath(ctx: Ctx, x: number, y: number, r: number): void {
  const w = r * 2.5, h = r * 1.9, c = r * 0.22;
  const L = x - w / 2, R = x + w / 2, B = y + h / 2;
  const top = y - h / 2 + h * 0.36;
  ctx.moveTo(L, top);
  ctx.lineTo(R, top);
  ctx.lineTo(R, B - c);
  ctx.arcTo(R, B, R - c, B, c);
  ctx.lineTo(L + c, B);
  ctx.arcTo(L, B, L, B - c, c);
  ctx.closePath();
}

/** The turned corner of a file page, drawn as a lighter triangle over the fill. */
export function cornerPath(ctx: Ctx, x: number, y: number, r: number): void {
  const w = r * 1.7, h = r * 2.15, f = w * 0.38;
  const R = x + w / 2, T = y - h / 2;
  ctx.moveTo(R - f, T);
  ctx.lineTo(R - f, T + f);
  ctx.lineTo(R, T + f);
  ctx.closePath();
}

/** Light details drawn over the fill in the "on" colour: robot eyes, rack bays, a cylinder's lid, a tag's hole. */
export function detailPath(ctx: Ctx, shape: Shape, x: number, y: number, r: number): number {
  switch (shape) {
    case "agent": {
      const ey = y + r * 0.08;
      ctx.moveTo(x - r * 0.42 + r * 0.2, ey);
      ctx.arc(x - r * 0.42, ey, r * 0.2, 0, Math.PI * 2);
      ctx.moveTo(x + r * 0.42 + r * 0.2, ey);
      ctx.arc(x + r * 0.42, ey, r * 0.2, 0, Math.PI * 2);
      return 1;
    }
    case "server":
      for (const dy of [-0.42, 0.42]) {
        roundRect(ctx, x - r * 0.62, y + dy * r - r * 0.17, r * 0.85, r * 0.34, r * 0.1);
        ctx.moveTo(x + r * 0.55 + r * 0.12, y + dy * r);
        ctx.arc(x + r * 0.55, y + dy * r, r * 0.12, 0, Math.PI * 2);
      }
      return 0.85;
    case "db": {
      const w = r * 1.9, e = r * 0.36, T = y - r * 1.1 + e;
      ctx.moveTo(x + w / 2, T);
      ctx.ellipse(x, T, w / 2, e, 0, 0, Math.PI * 2);
      return 0.35;
    }
    case "tag": {
      const hx = x - r * 1.25 + r * 0.72;
      ctx.moveTo(hx + r * 0.2, y);
      ctx.arc(hx, y, r * 0.2, 0, Math.PI * 2);
      return 0.9;
    }
    default:
      return 0;
  }
}

export function extensionOf(n: GNode): string {
  const ext = String(n.props.file_ext ?? (n.name.includes(".") ? n.name.split(".").pop() : "")).toLowerCase();
  return ext.length > 4 ? ext.slice(0, 4) : ext;
}

export interface Mark {
  text: string;
  font: string; // without size
  size: number; // world units
  dy: number; // vertical offset from the node centre, world units
}

/** What to print inside a node's icon, if anything. */
export function markFor(n: GNode, shape: Shape, collapsed: boolean, mono: string, sans: string): Mark | null {
  const r = n.r;
  switch (shape) {
    case "file": {
      const ext = extensionOf(n);
      if (!ext) return null;
      const size = r * (ext.length <= 2 ? 0.92 : ext.length === 3 ? 0.76 : 0.6);
      return { text: ext, font: `700 %s ${mono}`, size, dy: r * 0.2 };
    }
    case "dot":
      return { text: "ƒ", font: `italic 600 %s Georgia, "Times New Roman", serif`, size: r * 1.3, dy: r * 0.06 };
    case "diamond":
      return { text: "C", font: `700 %s ${sans}`, size: r * 0.95, dy: r * 0.04 };
    case "folder":
      return collapsed ? { text: "+", font: `700 %s ${sans}`, size: r * 1.25, dy: r * 0.16 } : null;
    case "step": {
      const t = String(n.props.order ?? "");
      return t ? { text: t, font: `700 %s ${sans}`, size: r * (t.length > 1 ? 0.9 : 1.05), dy: r * 0.04 } : null;
    }
    case "tag":
      return n.kind === "EnvGroup" ? { text: "$", font: `700 %s ${mono}`, size: r * 1.05, dy: r * 0.04 } : null;
    case "spark":
      return collapsed ? { text: "+", font: `700 %s ${sans}`, size: r * 0.9, dy: r * 0.04 } : null;
    default:
      return null;
  }
}

export function fontWithSize(m: Mark, px: number): string {
  return m.font.replace("%s", `${px}px`);
}

/** A small standalone icon for the legend, the details panel and search results. */
export function drawGlyph(ctx: Ctx, shape: Shape, size: number, color: string, onColor: string): void {
  const x = size / 2, y = size / 2;
  const r = shape === "folder" ? size * 0.33 : shape === "file" ? size * 0.38 : shape === "dot" ? size * 0.36
    : shape === "tag" || shape === "step" ? size * 0.36 : shape === "cloud" ? size * 0.34 : shape === "spark" ? size * 0.36
    : shape === "agent" ? size * 0.38 : shape === "server" || shape === "db" ? size * 0.4 : size * 0.33;
  ctx.beginPath();
  iconPath(ctx, shape, x, y, r);
  ctx.fillStyle = color;
  ctx.fill();
  ctx.beginPath();
  const alpha = detailPath(ctx, shape, x, y, r);
  if (alpha) {
    ctx.globalAlpha = alpha;
    ctx.fillStyle = onColor;
    ctx.fill();
    ctx.globalAlpha = 1;
  }
  if (shape === "file" || shape === "folder") {
    ctx.beginPath();
    if (shape === "file") cornerPath(ctx, x, y, r);
    else flapPath(ctx, x, y, r);
    ctx.globalAlpha = shape === "file" ? 0.45 : 0.2;
    ctx.fillStyle = onColor;
    ctx.fill();
    ctx.globalAlpha = 1;
  }
  const mark = shape === "dot" ? "ƒ" : shape === "diamond" ? "C" : "";
  if (mark) {
    const px = shape === "dot" ? r * 1.35 : r * 0.95;
    ctx.font = shape === "dot" ? `italic 600 ${px}px Georgia, "Times New Roman", serif` : `700 ${px}px system-ui, sans-serif`;
    ctx.fillStyle = onColor;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(mark, x, y + r * 0.06);
  }
}
