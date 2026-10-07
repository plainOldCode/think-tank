/* agent-board 초기화 스모크 (M4580A48-573W codex F1·F2):
   - 실제 agent-board.html + tt-agent-board.js 쌍을 vm으로 초기화 — ReferenceError 없이 로드·렌더
   - reply-only 스레드(루트가 목록 창 밖)도 루트 보완 후 렌더됨
   - esc 의존성: HTML이 tt-util.js를 로드하지 않음(자기완결)
   실행: node tests/smoke_agent_board.mjs */
import { readFileSync } from "node:fs";
import vm from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const staticDir = join(here, "..", "server", "static");
const html = readFileSync(join(staticDir, "agent-board.html"), "utf8");
const js = readFileSync(join(staticDir, "js", "tt-agent-board.js"), "utf8");

if (/tt-util\.js/.test(html)) {
  console.error("FAIL: agent-board.html이 tt-util.js를 로드 — 자기완결 깨짐");
  process.exit(1);
}
if (!/const abEsc = /.test(js)) {
  console.error("FAIL: 자체 esc(abEsc) 정의 없음");
  process.exit(1);
}

const el = () => ({
  innerHTML: "", value: "", checked: false, style: {},
  addEventListener() {}, focus() {},
});

const els = new Map();
const getEl = (id) => {
  if (!els.has(id)) els.set(id, el());
  return els.get(id);
};

const threadsFetched = [];
// F1 잔여: 이미 읽힌 메시지(reads 비었음 X)가 첫 로드에 섞여 있어야 esc 잔존 참조가 터진다
const listOnlyReplies = [
  { id: "MREPL-0002", thread_id: "MROOT-0001", author: "agy", body: "@codex 답변", mentions: ",codex,", created_at: "2026-10-05T09:01:00+0900", reads: ["codex"] },
  // M4A55K92-4BZK: 카드 ID(M+7-4) 링크·코드펜스·URL 렌더 — 실제 카드 ID 형식으로 검증(codex F1)
  { id: "MRENDER-0001", thread_id: null, author: "codex",
    body: "관련 카드 M4580A48-573W 확인.\n```\nls -la /path\n```\n문서 https://example.com/x 참고",
    mentions: "", created_at: "2026-10-05T09:02:00+0900", reads: [] },
];
const withRoot = [
  { id: "MROOT-0001", thread_id: null, author: "codex", body: "질문입니다", mentions: "", created_at: "2026-10-05T09:00:00+0900", reads: ["agy"] },
  { id: "MREPL-0002", thread_id: "MROOT-0001", author: "agy", body: "@codex 답변", mentions: ",codex,", created_at: "2026-10-05T09:01:00+0900", reads: ["codex"] },
];
// F2 잔여: 21개 스레드의 답글만 목록에 — 보완 상한 20을 넘어도 전체 렌더가 살아있어야 한다
for (let i = 0; i < 22; i++) {
  listOnlyReplies.push({ id: `MORPH-${String(i).padStart(4, "0")}`, thread_id: `MORPHR-${String(i).padStart(4, "0")}`,
    author: "codex", body: `답글 ${i}`, mentions: ",codex,", created_at: `2026-10-04T08:${String(i).padStart(2, "0")}:00+0900`, reads: [] });
}
const sandbox = {
  document: {
    getElementById: (id) => getEl(id),
  },
  fetch: (url) => {
    threadsFetched.push(url);
    if (url.includes("MORPHR-0000")) return Promise.reject(new Error("404")); // 조회 실패 스레드
    return Promise.resolve({
      ok: true,
      json: () => Promise.resolve(url.startsWith("/messages?thread=")
        ? (url.includes("MROOT-0001") ? withRoot : [{ id: "MORPHR-X", thread_id: null, author: "x", body: "루트", mentions: "", created_at: "2026-10-04T07:00:00+0900", reads: [] }])
        : listOnlyReplies),
      text: () => Promise.resolve(""),
    });
  },
  localStorage: { getItem: () => "", setItem() {} },
  alert: () => {},
  setInterval: () => {},
  console,
};
sandbox.window = sandbox;
vm.createContext(sandbox);

try {
  vm.runInContext(js, sandbox, { filename: "tt-agent-board.js" });
  await new Promise(r => setTimeout(r, 120)); // 비동기 초기화 완료 대기
} catch (e) {
  console.error("FAIL: 초기화 중 예외 —", e.message);
  process.exit(1);
}
if (vm.runInContext("typeof abEsc", sandbox) !== "function") {
  console.error("FAIL: abEsc 미정의");
  process.exit(1);
}
if (!threadsFetched.some(u => u.startsWith("/messages?thread="))) {
  console.error("FAIL: 루트 보완 조회(/messages?thread=) 미호출 — reply-only 스레드 누락");
  process.exit(1);
}
// M4A55K92-4BZK: 본문 렌더 — 카드 ID 링크(실제 형식 M+7-4), 코드펜스 <pre>, URL 앵커
const board = getEl("board").innerHTML;
if (!board.includes('<a href="/#M4580A48-573W">M4580A48-573W</a>')) {
  console.error("FAIL: 카드 ID가 /#상세 링크로 렌더되지 않음 — 정규식 길이 확인({7})");
  process.exit(1);
}
if (!board.includes("<pre>")) {
  console.error("FAIL: 코드펜스가 <pre>로 렌더되지 않음");
  process.exit(1);
}
if (!board.includes('href="https://example.com/x"')) {
  console.error("FAIL: URL이 앵커로 렌더되지 않음");
  process.exit(1);
}
console.log("smoke_agent_board OK — 자기완결 초기화 + 루트 보완 조회 + 본문 렌더 확인");
