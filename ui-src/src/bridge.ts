// Minimal MCP Apps (io.modelcontextprotocol/ui, spec 2026-01-26) client over postMessage.
// Hand-written instead of bundling @modelcontextprotocol/ext-apps (650 KB with zod) — it speaks the
// same JSON-RPC: ui/initialize → ui/notifications/initialized, then host notifications.

export const PROTOCOL_VERSION = "2026-01-26";

type Json = Record<string, any>;
type Handler = (params: any) => void;

export interface HostContext {
  theme?: "light" | "dark";
  styles?: { variables?: Record<string, string>; css?: { fonts?: string } };
  displayMode?: "inline" | "fullscreen" | "pip";
  availableDisplayModes?: string[];
  containerDimensions?: { height?: number; maxHeight?: number; width?: number; maxWidth?: number };
  platform?: string;
  safeAreaInsets?: { top: number; right: number; bottom: number; left: number };
  [k: string]: unknown;
}

export interface CallToolResult {
  content?: { type: string; text?: string }[];
  structuredContent?: Json;
  isError?: boolean;
}

export class Bridge {
  private nextId = 1;
  private pending = new Map<number | string, { resolve: (v: any) => void; reject: (e: any) => void }>();
  private handlers = new Map<string, Handler[]>();
  hostCapabilities: Json = {};
  hostContext: HostContext = {};
  hostInfo: Json = {};
  connected = false;

  constructor(private target: Window = window.parent) {
    window.addEventListener("message", (ev) => {
      if (ev.source !== this.target) return;
      const msg = ev.data;
      if (!msg || msg.jsonrpc !== "2.0") return;
      this.dispatch(msg);
    });
  }

  get embedded(): boolean {
    return this.target !== window;
  }

  on(method: string, fn: Handler): void {
    const list = this.handlers.get(method) ?? [];
    list.push(fn);
    this.handlers.set(method, list);
  }

  private post(msg: Json): void {
    this.target.postMessage(msg, "*");
  }

  request<T = any>(method: string, params: Json = {}, timeoutMs = 20000): Promise<T> {
    const id = this.nextId++;
    this.post({ jsonrpc: "2.0", id, method, params });
    return new Promise<T>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`${method} timed out`));
      }, timeoutMs);
      this.pending.set(id, {
        resolve: (v) => (clearTimeout(timer), resolve(v)),
        reject: (e) => (clearTimeout(timer), reject(e)),
      });
    });
  }

  notify(method: string, params: Json = {}): void {
    this.post({ jsonrpc: "2.0", method, params });
  }

  private dispatch(msg: Json): void {
    if (msg.id !== undefined && msg.method === undefined) {
      const p = this.pending.get(msg.id);
      if (!p) return;
      this.pending.delete(msg.id);
      if (msg.error) p.reject(new Error(msg.error.message ?? "request failed"));
      else p.resolve(msg.result);
      return;
    }
    if (typeof msg.method !== "string") return;
    if (msg.method === "ui/notifications/host-context-changed") {
      this.hostContext = { ...this.hostContext, ...(msg.params ?? {}) };
    }
    const handlers = this.handlers.get(msg.method) ?? [];
    let result: any = {};
    for (const h of handlers) result = h(msg.params ?? {}) ?? result;
    if (msg.id !== undefined) {
      // host → view requests we answer: ping, ui/resource-teardown, anything else is unknown
      if (msg.method === "ping" || msg.method === "ui/resource-teardown" || handlers.length) {
        this.post({ jsonrpc: "2.0", id: msg.id, result });
      } else {
        this.post({ jsonrpc: "2.0", id: msg.id, error: { code: -32601, message: `Method not found: ${msg.method}` } });
      }
    }
  }

  async connect(appInfo = { name: "repo-graph-view", version: "0.1.0" }): Promise<void> {
    const res = await this.request("ui/initialize", {
      appInfo,
      appCapabilities: { availableDisplayModes: ["inline", "fullscreen"] },
      protocolVersion: PROTOCOL_VERSION,
    }, 8000);
    this.hostCapabilities = res?.hostCapabilities ?? {};
    this.hostContext = res?.hostContext ?? {};
    this.hostInfo = res?.hostInfo ?? {};
    this.notify("ui/notifications/initialized", {});
    this.connected = true;
  }

  // ── view → host conveniences ──
  callTool(name: string, args: Json): Promise<CallToolResult> {
    return this.request("tools/call", { name, arguments: args }, 60000);
  }
  openLink(url: string): Promise<unknown> {
    return this.request("ui/open-link", { url });
  }
  sendMessage(text: string): Promise<unknown> {
    return this.request("ui/message", { role: "user", content: [{ type: "text", text }] });
  }
  updateModelContext(text: string): Promise<unknown> {
    return this.request("ui/update-model-context", { content: [{ type: "text", text }] });
  }
  requestDisplayMode(mode: "inline" | "fullscreen"): Promise<{ mode: string }> {
    return this.request("ui/request-display-mode", { mode });
  }
  sizeChanged(height: number): void {
    this.notify("ui/notifications/size-changed", { width: Math.ceil(window.innerWidth), height: Math.ceil(height) });
  }
}
