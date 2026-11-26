// Run after npm ci. Copy upstream browser builds verbatim, including licenses.
import { copyFileSync, mkdirSync, readFileSync } from "node:fs";
import { basename, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL("../", import.meta.url));
const assets = {
  marked: ["lib/marked.umd.js", "LICENSE"],
  "@highlightjs/cdn-assets": ["highlight.min.js", "styles/github-dark.min.css", "LICENSE"],
  dompurify: ["dist/purify.min.js", "LICENSE", "LICENSE-MPL"],
  pako: ["dist/browser/pako.umd.min.js", "LICENSE"],
};
const pins = JSON.parse(readFileSync(join(root, "package.json"), "utf8")).devDependencies;
for (const [name, files] of Object.entries(assets)) {
  const src = join(root, "node_modules", name);
  const { version } = JSON.parse(readFileSync(join(src, "package.json"), "utf8"));
  if (pins[name] !== version) throw new Error("Expected exact package pin: " + name);
  const prefix = name === "@highlightjs/cdn-assets" ? "highlightjs" : name;
  const dir = join(root, "src/trusted_router/static/vendor/chat", prefix + "-" + version);
  for (const file of files) {
    const source = join(src, file);
    const dest = join(dir, basename(file));
    if (process.argv.includes("--check")) {
      if (!readFileSync(dest).equals(readFileSync(source))) throw new Error("Vendored asset differs: " + dest);
    } else {
      mkdirSync(dir, { recursive: true });
      copyFileSync(source, dest);
    }
  }
}
