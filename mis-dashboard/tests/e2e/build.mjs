// Sandbox build for browser tests: bundles the REAL components (not Next.js) and compiles Tailwind v4 with its JS API.
import { createRequire } from "node:module";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const LOCAL = path.resolve(HERE, "../../node_modules");
const TOOLS = process.env.NPM_TOOLS ?? (fs.existsSync(path.join(LOCAL, "playwright")) ? LOCAL : "/opt/npm-tools/node_modules");
const require = createRequire(TOOLS + "/");
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const out = path.join(root, "tests/e2e/artifacts");
fs.mkdirSync(out, { recursive: true });

const esbuild = require("esbuild");
await esbuild.build({
  entryPoints: [path.join(root, "tests/e2e/entry.tsx")], bundle: true, outfile: path.join(out, "bundle.js"),
  format: "iife", platform: "browser", jsx: "automatic", nodePaths: [TOOLS], target: "es2022", minify: false, sourcemap: false,
  define: { "process.env.NODE_ENV": '"production"' }, logLevel: "warning",
});

const { compile } = require("tailwindcss");
const walk = (d) => fs.readdirSync(d, { withFileTypes: true }).flatMap((e) => (e.isDirectory() ? walk(path.join(d, e.name)) : [path.join(d, e.name)]));
const files = [...walk(path.join(root, "src")), ...walk(path.join(root, "app"))].filter((f) => /\.(tsx|ts)$/.test(f));
const candidates = new Set();
for (const f of files) for (const m of fs.readFileSync(f, "utf8").matchAll(/[A-Za-z0-9_\-:/.\[\]%#!@]+/g)) candidates.add(m[0]);
const resolveCss = (id, base) => {
  const p = id.startsWith(".") ? path.resolve(base, id) : id === "tailwindcss" ? path.join(TOOLS, "tailwindcss/index.css") : path.join(TOOLS, id.endsWith(".css") ? id : id + ".css");
  return { path: p, base: path.dirname(p), content: fs.readFileSync(p, "utf8") };
};
const compiler = await compile(fs.readFileSync(path.join(root, "app/globals.css"), "utf8"), { base: path.join(root, "app"), loadStylesheet: async (id, base) => resolveCss(id, base) });
fs.writeFileSync(path.join(out, "style.css"), compiler.build([...candidates]));
fs.writeFileSync(path.join(out, "index.html"),
  `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Lead MIS</title><link rel="stylesheet" href="/style.css"></head><body><div id="root"></div><script src="/bundle.js"></script></body></html>`);
console.log("built", fs.statSync(path.join(out, "bundle.js")).size, "bytes js,", fs.statSync(path.join(out, "style.css")).size, "bytes css");
