// index.html 진행 표시(활성 배너·카드 칩·상세 실행 이력) 런타임 스모크 — 계약 M3EREF97 §3.6.
// 실행: node tests/smoke_board_progress.mjs
//   본문: 활성 페이로드 렌더(queued/running/stalled 구분, blink, 진행률 없음), run_state='' 미표시,
//         빈 배열/조회 실패/초기 로딩 구분, 상세 실행 이력(finished 병기, failed≠status error), XSS 이스케이프.
//   --sub-after (SMOKE_AFTER=1 서브): 실행 종료 후 갱신 — 활성 목록 소멸(칩 제거) + 상세 finished 병기.
// 서버 코드 수정 없이 §2.2/§2.3 응답 형태(fixture: tests/fixtures/board_active.json)만으로 성립해야 한다.
import { readFileSync } from "fs";
import { spawnSync } from "child_process";
import { dirname, join } from "path";
import { fileURLToPath } from "url";
const here = dirname(fileURLToPath(import.meta.url));
const JS_FILES = ["tt-util.js", "tt-run.js", "tt-agents.js", "tt-board.js", "tt-detail.js", "tt-verify.js", "tt-main.js"];
let js = JS_FILES.map(f => readFileSync(join(here, "..", "server", "static", "js", f), "utf8")).join("\n")
  .replace("load().then(() => { if (location.hash) show(location.hash.slice(1)).catch(console.error); if (location.search.includes(\"agents\")) toggleAgents(); }).catch(console.error);", "/* boot removed */")
  .replace("setInterval(()=>load().catch(()=>{}), 5000);", "");

const isSub = process.argv.includes("--sub-after");
const nowIso = off => new Date(Date.now() + off).toISOString().replace(/\.\d+Z$/, "+0000");
const base = JSON.parse(readFileSync(join(here, "fixtures", "board_active.json"), "utf8"))
  .map(a => ({ ...a,
    last_progress_at: a.last_progress_at === "__FRESH__" ? nowIso(-5000)
      : a.last_progress_at === "__STALE__" ? nowIso(-600000) : a.last_progress_at }));
let ACTIVE_MODE = isSub ? "after" : "ok";   // ok | empty | err | after
const activePayload = () => ACTIVE_MODE === "after" ? base.filter(a => !["running", "stalled"].includes(a.run_state)) : base;

const issues = [
  { id: "M3EREF97-FXWQ", state: "in_progress", title: "runner 진행 관찰", labels: [],
    lease_by: "runner@macstudio", lease_expires: nowIso(36e5) },
  { id: "QW1111-AA22", state: "in_progress", title: "진행중 배치 작업", labels: [] },
  { id: "ZT3333-BB44", state: "in_progress", title: "무음 카드", labels: [] },
  { id: "OL5555-CC66", state: "in_progress", title: "오래된 진행 카드", labels: [] },
  { id: "ES7777-DD88", state: "todo", title: "주입 카드", labels: [] },
  { id: "NONE-0000", state: "todo", title: "실행없는 카드", labels: [] },
];
const dispatchRows = [
  { id: 57, issue_id: "M3EREF97-FXWQ", agent: "runner@macstudio", author: "board", message: "m",
    context: "", status: "ok", detail: "", ts: nowIso(-8e5),
    run_state: "queued", machine: "macstudio", session: "tt-runner@macstudio-57",
    started_at: nowIso(-8e5), last_progress_at: "", last_tail: "", ended_at: null },
  { id: 58, issue_id: "M3EREF97-FXWQ", agent: "runner@macstudio", author: "board",
    message: "m", context: "", status: "ok", detail: "", ts: nowIso(-7e5),
    run_state: isSub ? "finished" : "running", machine: "macstudio", session: "tt-runner@macstudio-58",
    started_at: nowIso(-7e5), last_progress_at: isSub ? "" : nowIso(-5000),
    last_tail: isSub ? "" : "<script>alert(1)</script>", ended_at: isSub ? nowIso(0) : null },
  { id: 62, issue_id: "M3EREF97-FXWQ", agent: "hook@x", author: "board", message: "m",
    context: "", status: "error", detail: "HTTP 500", ts: nowIso(-9e5),
    run_state: "", machine: "", session: "", started_at: null, last_progress_at: "", last_tail: "", ended_at: null },
  { id: 63, issue_id: "M3EREF97-FXWQ", agent: "runner@old", author: "board", message: "m",
    context: "", status: "ok", detail: "", ts: nowIso(-3e6),
    run_state: "finished", machine: "m1", session: "s63",
    started_at: nowIso(-3e6), last_progress_at: "", last_tail: "", ended_at: nowIso(-2e6) },
  { id: 64, issue_id: "M3EREF97-FXWQ", agent: "runner@old", author: "board", message: "m",
    context: "", status: "ok", detail: "", ts: nowIso(-12e5),
    run_state: "failed", machine: "m1", session: "s64",
    started_at: nowIso(-12e5), last_progress_at: "", last_tail: "", ended_at: nowIso(-11e5) },
];

const els = {};
const mk = id => els[id] ?? (els[id] = {
  id, innerHTML: "", textContent: "", value: "", checked: false, style: {}, dataset: {},
  classList: { add(){}, remove(){}, toggle(){}, contains: () => false },
  children: [], appendChild(ch) { this.children.push(ch); },
});
globalThis.document = {
  getElementById: mk,
  createElement: tag => ({ tag, innerHTML: "", className: "", style: {}, children: [],
    appendChild(ch) { this.children.push(ch); } }),
  activeElement: null,
};
globalThis.localStorage = { getItem: () => null, setItem() {} };
globalThis.location = { search: "", hash: "" };
globalThis.history = { replaceState() {} };
globalThis.alert = () => {};
globalThis.confirm = () => true;
globalThis.fetch = async u => {
  if (u.includes("/agents/active")) {
    if (ACTIVE_MODE === "err") return { ok: false, status: 500, json: async () => ({ detail: "boom" }) };
    if (ACTIVE_MODE === "empty") return { ok: true, json: async () => [] };
    return { ok: true, json: async () => structuredClone(activePayload()) };
  }
  if (u.startsWith("/agents")) return { ok: true, json: async () => [{ name: "runner@macstudio", enabled: 1 }] };
  if (u.includes("/dispatches")) return { ok: true, json: async () => structuredClone(dispatchRows) };
  if (u.includes("/tree")) return { ok: true, json: async () => ({ tree: [] }) };
  if (/\/issues\?/.test(u)) return { ok: true, json: async () => structuredClone(issues) };
  if (/\/issues\/[^/?]+$/.test(u)) return { ok: true, json: async () => ({
    id: "M3EREF97-FXWQ", title: "runner 진행 관찰", state: "in_progress", priority: 0, labels: [],
    assignee: "runner@macstudio", version: 1, created_at: nowIso(-9e6), body: "b", comments: [] }) };
  return { ok: true, json: async () => [] };
};
(0, eval)(js);
const { load, show, refreshActive, chipFor, renderRunSection } = globalThis;

// 초기 로딩: 수신 전 — 배너 미표시(빈 상태), 칩 없음(확정 진행률/빈 바 금지).
if (mk("runbanner").innerHTML !== "") throw new Error("초기 로딩에 배너가 표시됨");
if (chipFor("QW1111-AA22") !== "") throw new Error("미수신 상태에서 칩이 그려짐");

if (!isSub) {
  await refreshActive();
  if (!chipFor("QW1111-AA22").includes("run-running blink")) throw new Error("running fresh blink 없음: " + chipFor("QW1111-AA22"));
  if (!chipFor("OL5555-CC66").includes("run-running aging")) throw new Error("running stale(관측 오래됨) 구분 없음: " + chipFor("OL5555-CC66"));
  const q = chipFor("M3EREF97-FXWQ");
  if (!q.includes("run-queued") || !q.includes("57")) throw new Error("queued 칩/참조 없음: " + q);
  if (!chipFor("ZT3333-BB44").includes("run-stalled")) throw new Error("stalled 칩 없음: " + chipFor("ZT3333-BB44"));
  if (chipFor("NONE-0000") !== "") throw new Error("실행없는 카드(run_state 없음)에 칩 — 미표시 위반");
  for (const chip of base.map(a => chipFor(a.issue_id)).filter(Boolean))
    if (chip.includes("<progress") || /%|진행률/.test(chip)) throw new Error("빈 진행률 금지 위반: " + chip);

  await load();
  const banner = mk("runbanner").innerHTML;
  if (!/실행\(Active\) 5/.test(banner)) throw new Error("활성 배너 개수 없음: " + banner);
  if (!banner.includes("#57") || !banner.includes("Q1-AA22") && !banner.includes("M3EREF97-FXWQ"))
    throw new Error("배너에 카드 참조 없음: " + banner);
  if (banner.includes("<b>주입</b>")) throw new Error("XSS 미이스케이프(배너)");

  await show("M3EREF97-FXWQ");
  const d = els["dcontent"].innerHTML;
  if (!d.includes("tmux 실행")) throw new Error("상세 실행 섹션 없음");
  if (!/run-chip run-queued">queued/.test(d)) throw new Error("상세 queued 병기 없음");
  if (!d.includes("run-finished")) throw new Error("상세 finished 병기 없음");
  if (!d.includes("run-failed")) throw new Error("상세 failed 병기 없음");
  if (!d.includes("dispatch#62")) throw new Error("dispatch#62(전달 error)가 상세에 있어야 하나 누락");
  if (/run-chip[^>]*>./.test(/>62<\/|62[^<]*run-chip/)) throw new Error("status error 전용 행에 run 칩 금지");
  const row62 = /dispatch#62[\s\S]{0,200}/.exec(d);
  if (row62 && /run-chip/.test(row62[0])) throw new Error("run_state=''(status error 전용) 행에 run 칩 표시됨");
  if (d.includes("<script>alert(1)")) throw new Error("상세 tail XSS 미이스케이프");
  if (!d.includes("러너 실행 실패와 별개")) throw new Error("failed≠status error 구분 라벨 누락");
  if (d.includes("<progress") || /진행률/.test(d)) throw new Error("상세 진행률 금지 위반");

  ACTIVE_MODE = "empty"; await refreshActive();
  if (!/tmux 실행 없음/.test(mk("runbanner").innerHTML)) throw new Error("빈 배열 문구 없음");
  if (chipFor("QW1111-AA22") !== "") throw new Error("빈 배열인데 칩 잔존");

  // API 오류: 조용한 폴링(throw 없음), 마지막 데이터 보존 + 실패 표시, 보드 렌더 계속
  await load();
  ACTIVE_MODE = "err";
  try { await refreshActive(); } catch (e) { throw new Error("조회 실패가 throw 됨(조용한 폴링 위반)"); }
  if (!/조회 실패/.test(mk("runbanner").innerHTML)) throw new Error("API 오류 표시 없음");
  if (!mk("board").children.length) throw new Error("오류 시 보드 렌더 중단");

  const r = spawnSync(process.execPath, [fileURLToPath(import.meta.url), "--sub-after"],
    { env: { ...process.env }, encoding: "utf8" });
  if (r.status !== 0) throw new Error("after-run failed: " + (r.stderr || r.stdout).slice(0, 400));
  console.log("smoke board-progress ok (본문 + after 서브)");
} else {
  // 서브: 실행 종료 후 갱신 — 활성 소멸(칩 제거) + 상세 finished 병기
  await refreshActive();
  if (chipFor("QW1111-AA22") !== "") throw new Error("종료 후 running 칩 잔존");
  if (chipFor("ZT3333-BB44") !== "") throw new Error("종료 후 stalled 칩 잔존");
  if (!chipFor("M3EREF97-FXWQ").includes("run-queued")) throw new Error("미종료 queued 칩까지 사라짐");
  await show("M3EREF97-FXWQ");
  const d = els["dcontent"].innerHTML;
  if (!/run-chip run-finished">finished/.test(d)) throw new Error("종료 후 상세 finished 병기 없음");
  console.log("after ok");
}
