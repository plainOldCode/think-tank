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

const threadsFetched = [];
const listOnlyReplies = [
  { id: "MREPL-0002", thread_id: "MROOT-0001", author: "agy", body: "@codex 답변", mentions: ",codex,", created_at: "2026-10-05T09:01:00+0900", reads: [] },
];
const withRoot = [
  { id: "MROOT-0001", thread_id: null, author: "codex", body: "질문입니다", mentions: "", created_at: "2026-10-05T09:00:00+0900", reads: [] },
  { id: "MREPL-0002", thread_id: "MROOT-0001", author: "agy", body: "@codex 답변", mentions: ",codex,", created_at: "2026-10-05T09:01:00+0900", reads: [] },
];
const sandbox = {
  document: {
    getElementById: () => el(),
  },
  fetch: (url) => {
    threadsFetched.push(url);
    return Promise.resolve({
      ok: true,
      json: () => Promise.resolve(url.startsWith("/messages?thread=") ? withRoot : listOnlyReplies),
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
console.log("smoke_agent_board OK — 자기완결 초기화 + 루트 보완 조회 확인");
