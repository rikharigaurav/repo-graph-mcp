// Node icons, shared by the map (canvas, world units) and the legend/panel glyphs.
//   folder   → folder with a tab; a folded folder shows "+"
//   file     → page with a turned corner; the extension is printed inside when it's legible
//   dot      → function: round badge with "ƒ"
//   diamond  → class: diamond with "C"
//   hex      → API endpoint
import type { GNode, Shape } from "./model";

type Ctx = CanvasRenderingContext2D;

/** Half the drawn width, in units of r: used for label offsets, hit tests, collisions and arrow tips. */
export function halfWidth(shape: Shape, r: number): number {
  switch (shape) {
    case "folder": return r * 1.25;
    case "file": return r * 0.85;
    case "diamond": return r * 1.15;
    case "hex": return r * 1.1;
    default: return r;
  }
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
  const r = shape === "folder" ? size * 0.33 : shape === "file" ? size * 0.38 : shape === "dot" ? size * 0.36 : size * 0.33;
  ctx.beginPath();
  iconPath(ctx, shape, x, y, r);
  ctx.fillStyle = color;
  ctx.fill();
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
