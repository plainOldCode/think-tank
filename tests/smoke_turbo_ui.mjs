// Turbo board smoke without a browser: ink page roots, escaping, grouping.
import { readFileSync } from "node:fs";
import vm from "node:vm";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, "..", "turbo", "static");
const html = readFileSync(join(root, "index.html"), "utf8");
const js = readFileSync(join(root, "turbo.js"), "utf8");
const css = readFileSync(join(root, "turbo.css"), "utf8");

if (!html.includes('id="archive"') || !html.includes('id="turbo"') || !html.includes('id="stuck"')) {
  throw new Error("archive, turbo, and stuck roots missing");
}
if (html.includes("tmux") || js.includes("tmux")) {
  throw new Error("tmux leaked into the turbo page");
}
if (!css.includes("#f7f6f3") || !css.includes("#c23b22")) {
  throw new Error("ink colors missing");
}

const context = vm.createContext({
  document: { getElementById() { return null; } },
  localStorage: { getItem() { return null; }, setItem() {} },
});
vm.runInContext(js, context);
if (context.esc("a<b&") !== "a&lt;b&amp;") throw new Error("esc failed: " + context.esc("a<b&"));
const grouped = context.groupByState(
  [{ state: "todo", id: "1" }, { state: "nope", id: "2" }],
  ["todo", "done"],
);
if (grouped.groups.todo.length !== 1 || grouped.rest.length !== 1) {
  throw new Error("groupByState failed");
}
const stuck = context.stuckHtml([{ id: "T1", title: "a", age_s: 10, reason: "handoff" }]);
if (!stuck.includes("T1") || !stuck.includes("handoff")) {
  throw new Error("stuckHtml failed: " + stuck);
}
if (context.stuckHtml([]) !== "") throw new Error("empty stuckHtml should be blank");
if (!context.answerNote({ confirm: true }).includes("confirm")) {
  throw new Error("confirm note missing");
}
if (!context.answerNote({ confirm: false, answer: "" }).includes("기본값")) {
  throw new Error("default note missing");
}
console.log("turbo ui smoke ok");
