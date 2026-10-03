// 회귀: 현재 서버(백엔드 미구현 — dispatches에 run_* 필드 없음, /agents/active 404)에서도
// 진행 표시가 조용해야 한다(빈 진행률·오류 배너 없음). 서빙 코드 수정 없이 하위 호환이어야 함.
// 실행: node tests/smoke_board_legacy_quiet.mjs
import { readFileSync } from "fs";
import { dirname, join } from "path";
import { fileURLToPath } from "url";
const here = dirname(fileURLToPath(import.meta.url));
const JS_FILES = ["tt-util.js", "tt-run.js", "tt-agents.js", "tt-board.js", "tt-detail.js", "tt-verify.js", "tt-main.js"];
const js = JS_FILES.map(f => readFileSync(join(here, "..", "server", "static", "js", f), "utf8")).join("\n")
  .replace("load().then(() => { if (location.hash) show(location.hash.slice(1)).catch(console.error); if (location.search.includes(\"agents\")) toggleAgents(); }).catch(console.error);", "/* boot */")
  .replace("setInterval(()=>load().catch(()=>{}), 5000);", "");

const els = {};
const mk = id => els[id] ?? (els[id] = {
  id, innerHTML: "", textContent: "", value: "", checked: false, style: {}, dataset: {},
  classList: { add(){}, remove(){}, toggle(){}, contains: () => false },
  children: [], appendChild(ch) { this.children.push(ch); },
});
globalThis.document = {
  getElementById: mk,
  createElement: t => ({ tag: t, innerHTML: "", className: "", style: {}, children: [], appendChild(ch) { this.children.push(ch); } }),
  activeElement: null,
};
globalThis.localStorage = { getItem: () => null, setItem() {} };
globalThis.location = { search: "", hash: "" };
globalThis.history = { replaceState() {} };
globalThis.alert = () => {};
globalThis.fetch = async u => {
  if (u.includes("/agents/active")) return { ok: false, status: 404, json: async () => ({ detail: "not found" }) };
  if (u.startsWith("/agents")) return { ok: true, json: async () => [] };
  if (u.includes("/dispatches")) return { ok: true, json: async () => [{ id: 1, issue_id: "X", agent: "a", author: "human", message: "m", context: "", status: "ok", detail: "", ts: "2026-09-29T10:00:00+0900" }] };
  if (u.includes("/tree")) return { ok: true, json: async () => ({ tree: [] }) };
  if (/\/issues\?/.test(u)) return { ok: true, json: async () => [{ id: "X", state: "todo", title: "t", labels: [] }] };
  return { ok: true, json: async () => ({ id: "X", title: "t", state: "todo", priority: 0, labels: [], version: 1, created_at: "2026-09-29T09:00:00+0900", body: "b", comments: [] }) };
};
(0, eval)(js);
await load();
await show("X");
const d = els["dcontent"].innerHTML;
if (d.includes("tmux 실행")) throw new Error("run_* 필드 없는 서버 응답에 실행 섹션이 표시됨(빈 데이터 노출)");
if (mk("runbanner").innerHTML.includes("실행(Active)")) throw new Error("404인데 활성 배너 표시");
console.log("legacy-server quiet ok");
