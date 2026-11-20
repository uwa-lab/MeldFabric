import { readFileSync, readdirSync } from "node:fs";
import { resolve, join } from "node:path";

const root = resolve(import.meta.dirname, "../src");
const styleRoot = join(root, "styles");
const css = readdirSync(styleRoot)
  .filter((name) => name.endsWith(".css"))
  .sort()
  .map((name) => readFileSync(join(styleRoot, name), "utf8"))
  .join("\n");
const rootBlock = css.match(/:root\s*\{([^}]*)\}/s)?.[1] || "";
const body = css.replace(/:root\s*\{[^}]*\}/s, "");
const failures = [];
const tokens = new Set([...rootBlock.matchAll(/(--[\w-]+)\s*:/g)].map((match) => match[1]));
for (const match of body.matchAll(/var\((--[\w-]+)/g))
  if (!tokens.has(match[1])) failures.push(`使用了未定义的设计变量：${match[1]}`);
if (/#[\da-f]{3,8}\b|rgba?\(|hsla?\(/i.test(body))
  failures.push("颜色必须使用 :root 中的语义变量。");
for (const name of ["font-size", "border-radius"])
  for (const match of body.matchAll(new RegExp(`${name}:\\s*([^;]+);`, "g")))
    if (!/^var\(--[\w-]+\)$/.test(match[1])) failures.push(`${name} 必须复用设计变量：${match[1]}`);
function visit(folder) {
  for (const entry of readdirSync(folder, { withFileTypes: true })) {
    const file = join(folder, entry.name);
    if (entry.isDirectory()) visit(file);
    else if (entry.name.endsWith(".vue")) {
      const text = readFileSync(file, "utf8");
      if (/<style\b|\s:?(?:style)=/i.test(text))
        failures.push(`${entry.name}: 样式请放入 styles 下对应的组件样式文件并复用设计变量。`);
      if (/<button\b[^>]*class="[^"]*\b(?:primary|secondary|danger)\b/i.test(text))
        failures.push(`${entry.name}: 标准操作按钮必须复用 UiButton。`);
      const pairedButtons = text.replace(/<UiButton\b[^>]*\/>/gs, "");
      for (const match of pairedButtons.matchAll(/<UiButton\b([^>]*)>([\s\S]*?)<\/UiButton\s*>/g)) {
        const destructive = /删除|移除/.test(`${match[1]} ${match[2]}`);
        if (destructive && !/variant="danger"/.test(match[1]))
          failures.push(`${entry.name}: 删除类操作必须使用 danger 语义。`);
      }
      if (entry.name !== "ModalFrame.vue" && /<MotionDialog\b/.test(text))
        failures.push(`${entry.name}: 业务弹窗必须复用 ModalFrame。`);
    }
  }
}
visit(root);
if (failures.length) {
  console.error(failures.join("\n"));
  process.exit(1);
}
console.log("Design tokens and component style boundaries verified.");
