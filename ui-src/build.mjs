// Bundles the view into ONE self-contained HTML file (MCP App iframes allow inline code only
// unless extra CSP domains are declared), written into the Python package.
import { build, context } from "esbuild";
import { readFile, writeFile, mkdir } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const out = resolve(here, "../src/repo_graph_mcp/ui/graph-view.html");
const watch = process.argv.includes("--watch");

const options = {
  entryPoints: [resolve(here, "src/main.ts")],
  bundle: true,
  minify: !watch && process.env.MINIFY !== "0",
  format: "iife",
  target: "es2020",
  write: false,
  legalComments: "none",
};

async function emit(result) {
  const js = result.outputFiles[0].text.replace(/<\/script/gi, "<\\/script");
  const css = await readFile(resolve(here, "src/styles.css"), "utf8");
  const html = (await readFile(resolve(here, "src/index.html"), "utf8"))
    .replace("/*__STYLES__*/", () => css)
    .replace("/*__SCRIPT__*/", () => js);
  await mkdir(dirname(out), { recursive: true });
  await writeFile(out, html);
  console.log(`wrote ${out} (${(html.length / 1024).toFixed(1)} KB)`);
}

if (watch) {
  const ctx = await context({ ...options, plugins: [{ name: "emit", setup(b) { b.onEnd((r) => r.errors.length || emit(r)); } }] });
  await ctx.watch();
} else {
  await emit(await build(options));
}
