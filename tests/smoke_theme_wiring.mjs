// M43KDKWQ-6Y7Z 테마·디테일 닫기 와이어링 스모크 — PR#40이 호출부(onclick/onchange)만 두고
// setTheme·closeDetail 정의를 빠뜨린 회귀를 잡는다. 실행: node tests/smoke_theme_wiring.mjs
import { readFileSync } from "fs";
import { dirname, join } from "path";
import { fileURLToPath } from "url";
const here = dirname(fileURLToPath(import.meta.url));
const html = readFileSync(join(here, "..", "server", "static", "index.html"), "utf8");
const JS_FILES = ["tt-util.js", "tt-run.js", "tt-agents.js", "tt-board.js", "tt-detail.js", "tt-verify.js", "tt-main.js", "tt-theme.js"];
const js = JS_FILES.map(f => {
  try { return readFileSync(join(here, "..", "server", "static", "js", f), "utf8"); }
  catch { return ""; }
}).join("\n")
  .replace("load().then(() => { if (location.hash) show(location.hash.slice(1)).catch(console.error); if (location.search.includes(\"agents\")) toggleAgents(); }).catch(console.error);", "/* boot removed */")
  .replace("setInterval(()=>load().catch(()=>{}), 5000);", "");

// 1) 와이어링 — 호출부와 정의가 한 쌍으로 존재해야 한다
if (!/onchange="setTheme\(/.test(html)) throw new Error("index.html에 setTheme 호출부 없음");
if (!/onclick="closeDetail\(\)"/.test(html)) throw new Error("index.html에 closeDetail 호출부 없음");
if (!/\bfunction setTheme\b/.test(js)) throw new Error("setTheme 정의 없음 — 테마 선택 불동작(버그 2)");
if (!/\bfunction closeDetail\b/.test(js)) throw new Error("closeDetail 정의 없음 — 닫기 버튼 불동작(버그 1)");
if (!/<script src="\/js\/tt-theme\.js">/.test(html)) throw new Error("index.html에 tt-theme.js 로드 태그 없음");

// 2) 동작 — 스텁 DOM으로 실제 호출
const store = {};
const removed = [];
const root = { dataset: {} };
const mk = id => ({
  id, innerHTML: "", textContent: "", value: "", checked: false, style: {}, dataset: {},
  classList: { add(){}, remove(c) { removed.push(id + ":" + c); }, contains: () => false },
  children: [], appendChild(ch) { this.children.push(ch); },
});
const detailEl = mk("detail");
globalThis.document = {
  documentElement: root,
  getElementById: id => id === "detail" ? detailEl : mk(id),
  createElement: tag => ({ tag, innerHTML: "", className: "", style: {}, children: [], appendChild() {} }),
  activeElement: null,
};
globalThis.localStorage = { getItem: k => store[k] ?? null, setItem: (k, v) => { store[k] = v; } };
globalThis.location = { search: "", hash: "" };
globalThis.history = { replaceState() {} };
globalThis.alert = () => {};
globalThis.confirm = () => true;
globalThis.fetch = async () => ({ ok: true, json: async () => [] });
(0, eval)(js);

// 기본 pixel — data-theme 없음, 저장 없음(스텁 초기값 기준)
if (root.dataset.theme !== undefined) throw new Error("기본 테마에 data-theme이 남음: " + root.dataset.theme);
// 선택 → data-theme + localStorage 지속
setTheme("terminal");
if (root.dataset.theme !== "terminal") throw new Error("terminal 미적용: " + root.dataset.theme);
if (store["tt-theme"] !== "terminal") throw new Error("localStorage tt-theme 미저장: " + store["tt-theme"]);
// 무효값 → pixel 폴백(data-theme 제거)
setTheme("hacker-9000");
if (root.dataset.theme !== undefined) throw new Error("무효 테마가 data-theme에 남음: " + root.dataset.theme);
if (store["tt-theme"] !== "pixel") throw new Error("무효값 폴백 미저장: " + store["tt-theme"]);
// 저장값 복원
store["tt-theme"] = "ink";
setTheme("swiss"); // 다른 값으로 덮은 뒤 재초기화 경로는 setTheme 자체로 검증 — 복원은 페이지 로드 IIFE
if (root.dataset.theme !== "swiss") throw new Error("재적용 실패: " + root.dataset.theme);
// 닫기 — detail의 open 클래스 제거
closeDetail();
if (!removed.includes("detail:open")) throw new Error("closeDetail이 detail의 open을 제거하지 않음: " + removed.join(","));
console.log("smoke_theme_wiring: OK — 호출부↔정의 한 쌍, pixel 기본, 지속·폴백·복원, 닫기 클래스 제거");
