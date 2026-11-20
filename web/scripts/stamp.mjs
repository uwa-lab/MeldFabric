import { createHash } from "node:crypto";
import { readdirSync, readFileSync, writeFileSync } from "node:fs";
import { resolve, relative } from "node:path";
const root = resolve(import.meta.dirname, "..");
function walk(dir) {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = resolve(dir, entry.name);
    return entry.isDirectory() ? walk(path) : [path];
  });
}
const sources = [
  ...walk(resolve(root, "src")),
  ...walk(resolve(root, "scripts")),
  ...["index.html", "package.json", "package-lock.json", "vite.config.js"].map((name) =>
    resolve(root, name),
  ),
].sort();
const hash = createHash("sha256");
for (const path of sources) {
  hash.update(relative(root, path).replaceAll("\\", "/") + "\0");
  hash.update(readFileSync(path));
  hash.update("\0");
}
writeFileSync(
  resolve(root, "../src/meldfabric/management/static/build.json"),
  JSON.stringify({ source_sha256: hash.digest("hex") }, null, 2) + "\n",
);
