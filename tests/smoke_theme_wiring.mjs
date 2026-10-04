// M43KDKWQ-6Y7Z 테마·디테일 닫기 스모크 — 실제 구현(tt-util.js의 setTheme·tt-detail.js의 closeDetail)을
// 격리 컨텍스트에서 검증한다. codex 리뷰 F2 반영: 저장값 복원은 새 실행 컨텍스트에서 초기화해 단언한다.
// 스테일 캐시 혼합(진짜 원인)은 pytest test_정적_자산은_no_cache로_서빙된다가 담당.
import { readFileSync } from "fs";
import { dirname, join } from "path";
import { fileURLToPath } from "url";
import vm from "vm";
const here = dirname(fileURLToPath(import.meta.url));
const util = readFileSync(join(here, "..", "server", "static", "js", "tt-util.js"), "utf8");
const detail = readFileSync(join(here, "..", "server", "static", "js", "tt-detail.js"), "utf8");
const html = readFileSync(join(here, "..", "server", "static", "index.html"), "utf8");

// 와이어링 — index.html의 호출부와 실제 정의가 한 쌍이고, 중복 정의 모듈이 없어야 한다
if (!/onchange="setTheme\(/.test(html)) throw new Error("index.html에 setTheme 호출부 없음");
if (!/onclick="closeDetail\(\)"/.test(html)) throw new Error("index.html에 closeDetail 호출부 없음");
if (!/\bfunction setTheme\b/.test(util)) throw new Error("tt-util.js에 setTheme 정의 없음");
if (!/\bfunction closeDetail\b/.test(detail)) throw new Error("tt-detail.js에 closeDetail 정의 없음");
if (/tt-theme\.js/.test(html)) throw new Error("중복 정의 모듈 tt-theme.js가 남아 있음 — 원본과 이중 정의");

const mkEl = id => ({
  id, innerHTML: "", textContent: "", value: "", checked: false, style: {}, dataset: {},
  attrs: {},
  setAttribute(k, v) { this.attrs[k] = v; },
  removeAttribute(k) { delete this.attrs[k]; },
  classList: { add(c) { (this._cls ??= new Set()).add(c); }, remove(c) { this._removed = (this._removed ?? new Set()).add(c); }, contains() { return false; } },
  children: [], appendChild(ch) { this.children.push(ch); },
});
const mkDoc = store => {
  const els = {};
  return {
    documentElement: { attrs: {}, setAttribute(k, v) { this.attrs[k] = v; }, removeAttribute(k) { delete this.attrs[k]; } },
    getElementById(id) { return els[id] ??= mkEl(id); },
    createElement(tag) { return mkEl(tag); },
    activeElement: null,
    __store: store,
  };
};
const ctx = (store, extraFetch) => {
  const doc = mkDoc(store);
  return vm.createContext({
    document: doc, localStorage: { getItem: k => store[k] ?? null, setItem: (k, v) => { store[k] = v; } },
    location: { search: "", hash: "" }, history: { replaceState() {} },
    alert: () => {}, confirm: () => true,
    fetch: extraFetch ?? (async () => ({ ok: true, json: async () => [] })),
    console,
  });
};

// 1) 동작 — 8종 선택, 지속, 무효 폴백, 닫기
{
  const store = {};
  const c = ctx(store);
  vm.runInContext(util + "\n" + detail, c);
  const root = c.document.documentElement;
  c.setTheme("terminal");
  if (root.attrs["data-theme"] !== "terminal") throw new Error("terminal 미적용: " + root.attrs["data-theme"]);
  if (store["tt-theme"] !== "terminal") throw new Error("localStorage tt-theme 미저장: " + store["tt-theme"]);
  c.setTheme("hacker-9000");
  if ("data-theme" in root.attrs) throw new Error("무효 테마가 data-theme에 남음");
  if (store["tt-theme"] !== "pixel") throw new Error("무효값 폴백 미저장: " + store["tt-theme"]);
  const sel = c.document.getElementById("theme");
  if (sel.value !== "pixel") throw new Error("셀렉터 동기화 실패: " + sel.value);
  for (const t of ["ink", "newsprint", "terminal", "swiss", "ticket", "blueprint", "soft"]) {
    c.setTheme(t);
    if (root.attrs["data-theme"] !== t) throw new Error(t + " 적용 실패");
  }
  c.setTheme("pixel");
  if ("data-theme" in root.attrs) throw new Error("pixel 복귀 시 data-theme 잔존");
  const detailEl = c.document.getElementById("detail");
  detailEl.classList.add("open");
  c.closeDetail();
  if (!detailEl.classList._removed?.has("open")) throw new Error("closeDetail이 open을 제거하지 않음");
}

// 2) 저장값 복원 — codex F2: 새 페이지(새 컨텍스트)에서 초기화해야 단언된다
{
  const store = { "tt-theme": "ink" };
  const c = ctx(store);
  vm.runInContext(util, c);   // tt-util.js는 로드 시 initTheme()를 스스로 실행
  const root = c.document.documentElement;
  if (root.attrs["data-theme"] !== "ink") throw new Error("복원 실패 — data-theme: " + JSON.stringify(root.attrs));
  const sel = c.document.getElementById("theme");
  if (sel.value !== "ink") throw new Error("복원 실패 — 셀렉터: " + sel.value);
  // 변이 감지: initTheme 제거 시 이 검사는 실패해야 한다 (initTheme 정의 존재 확인으로 고정)
  if (!/\bfunction initTheme\b/.test(util)) throw new Error("initTheme 없음 — 복원 경로 상실");
}
// 3) 스테일 캐시 재현 — 실제 사용자 실패(버그 1·2)의 발생 경로.
// 배포 전 tt-util.js(git 339a00f)에는 setTheme이 0회 등장하고 StaticFiles 응답엔 Cache-Control이
// 없었다 — 낡은 JS 캐시 + 신규 index.html 호출부 혼합 시 ReferenceError로 버튼이 죽는다.
{
  // git 339a00f의 실제 tt-util.js 내용(전체 461줄 중 setTheme 0회 확인)을 그대로 로드한다.
  // 파일이 없으면(예: 히스토리 리라이트 후) 건너뛰지 않고 명확히 실패한다 — 근거 고정용 고정 경로.
  const staleUtil = readFileSync(join(here, "..", "tests", "fixtures", "old_tt_util_339a00f.js"), "utf8");
  if (/setTheme/.test(staleUtil)) throw new Error("픽스처가 구버전이 아님 — setTheme이 존재");
  const stale = ctx({});
  vm.runInContext(staleUtil, stale);
  let caught;
  try { vm.runInContext("setTheme('terminal')", stale); }
  catch (e) { caught = e; }
  if (!caught || !/setTheme is not defined/.test(String(caught)))
    throw new Error("스테일 캐시 재현 실패: " + caught);
  let caught2;
  try { vm.runInContext("closeDetail()", stale); }
  catch (e) { caught2 = e; }
  if (!caught2 || !/closeDetail is not defined/.test(String(caught2)))
    throw new Error("닫기 스테일 재현 실패: " + caught2);
}
console.log("smoke_theme_wiring: OK — 호출부↔정의 한 쌍(원본 단일 정의), 8종 적용·지속·폴백·닫기, 새 컨텍스트 복원, 스테일 캐시 ReferenceError 재현");
