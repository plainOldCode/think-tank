// mobile.html 스크립트 런타임 스모크 (브라우저 없는 대체 검증).
// 실행: node tests/smoke_mobile.mjs
//   본문: 저장된 ME 시나리오(렌더/스킵/서브토글/시트/dispatch 페이로드)
//   SMOKE_NO_ME=1 서브: ME 미저장 — 시트 내 입력 필드 → 선택 시 자동 저장 → dispatch author
import { readFileSync } from "fs";
import { spawnSync } from "child_process";
import { dirname, join } from "path";
import { fileURLToPath } from "url";
const here = dirname(fileURLToPath(import.meta.url));
const html = readFileSync(join(here, "..", "server", "static", "mobile.html"), "utf8");
const js = html.match(/<script>([\s\S]*?)<\/script>/)[1].replace("load(); setInterval(load, 6000);", "");
const noMe = !!process.env.SMOKE_NO_ME;
const store = noMe ? {} : { "tt-m-me": "me@x" };
globalThis.localStorage = { getItem: k => store[k] ?? null, setItem: (k, v) => store[k] = v };
const els = {};
const bodyClasses = new Set();
globalThis.document = {
  body: { classList: { add: c => bodyClasses.add(c), remove: c => bodyClasses.delete(c), contains: c => bodyClasses.has(c) } },
  querySelector: s => els[s] ?? (els[s] = { innerHTML: "", textContent: "", value: "", style: {}, focus() {}, classList: { add(){}, remove(){}, toggle(){}, contains: () => false } }),
};
globalThis.CSS = { escape: s => s };
const lease = new Date(Date.now() + 36e5).toISOString().slice(0, 19) + "+0000";
const two = [
  { id: "A1", state: "todo", title: "t", labels: [] },
  { id: "B2", state: "review", title: "r", labels: [], version: 1 },
];
const three = [
  { id: "M3", state: "in_progress", title: "mine", labels: [], assignee: "me@x", lease_by: "me@x", lease_expires: lease },
  { id: "E5", state: "in_progress", title: "expired", labels: [], assignee: "other@gx", lease_by: "other@gx", lease_expires: "2026-01-01T00:00:00+0900" },
  { id: "O4", state: "in_progress", title: "nolease", labels: [], assignee: "someone" },
];
let n = 0;
const calls = [];
globalThis.fetch = async (u, o) => {
  calls.push([u, o?.body]);
  if (u.includes("/agents")) return { ok: true, json: async () => [{ name: "agent-1@host1", enabled: 1, last_ok: "2026-09-29T10:00:00+0900" }] };
  if (!u.startsWith("/issues")) return { ok: true, json: async () => [] };
  return { ok: true, json: async () => structuredClone(n++ < 2 ? two : three) };
};
(0, eval)(js);
const { load, pick, startMenu, pickFromSheet, setSub, sendDispatch, pass, passConfirm } = globalThis;

await load();
if (!noMe) {
  if (!els["#list"].innerHTML.includes("B2")) throw new Error("1st render broken");
  pick("todo");
  if (!els["#list"].innerHTML.includes("onclick=\"startMenu('A1')\"")) throw new Error("start 버튼 배선 누락(구버전 잔존)");
  pick("review");
  const before = els["#list"].innerHTML;
  await load();
  if (els["#list"].innerHTML !== before) throw new Error("identical data re-rendered");
  await load();
  if (!els["#list"].innerHTML.includes("empty")) throw new Error("3rd render not applied");
  pick("in_progress");
  const list = els["#list"].innerHTML;
  if (!list.includes("class=\"card mine\"")) throw new Error("mine class missing: " + list.slice(0, 150));
  if (!list.includes("E5") || !list.includes("lease 만료")) throw new Error("만료 점유 누락");
  if (list.includes("O4")) throw new Error("lease 없는 카드가 청구중 노출");
  setSub("all");
  if (!els["#list"].innerHTML.includes("O4") || !els["#list"].innerHTML.includes("card other")) throw new Error("all 누락");
  setSub("lease");
  startMenu("A1");
  const sheet = els["#sheet"].innerHTML;
  if (!sheet.includes("agent-1@host1") || !sheet.includes("내가 진행")) throw new Error("start menu missing agents");
  if (!sheet.includes("onclick=\"pickFromSheet('agent-1@host1')\"")) throw new Error("agent 버튼 배선 누락");
  if (!bodyClasses.has("sheet-open")) throw new Error("배경 스크롤 잠금 누락");
  if (!sheet.includes("id=\"mein\"") && !store["tt-m-me"]) throw new Error("mein 필드 있어야 함(ME 저장돼 있으면 생략 정상)");
  await pickFromSheet("agent-1@host1");
  if (!els["#sheet"].innerHTML.includes("id=\"dmsg\"")) throw new Error("step2 dispatch sheet 없음");
  els["#dmsg"] = els["#dmsg"] || { value: "" };
  els["#dmsg"].value = "이 카드를 진행해주세요";
  await sendDispatch("A1", "agent-1@host1");
  if (bodyClasses.has("sheet-open")) throw new Error("closeSheet 잠금 해제 누락");
  const d = calls.find(([u]) => u.includes("/dispatch"));
  if (!d) throw new Error("dispatch not called");
  if (!d[1].includes('"agent":"agent-1@host1"') || !d[1].includes("me@x")) throw new Error("dispatch payload wrong: " + d[1]);
  pass("B2", 1);
  if (!els["#sheet"].innerHTML.includes("id=\"pev\"")) throw new Error("pass sheet 없음");
  els["#pev"] = els["#pev"] || { value: "" };
  els["#pev"].value = "스모크 통과 확인";
  await passConfirm(1);
  const v = calls.find(([u]) => u.includes("/verify"));
  if (!v || !v[1].includes("me@x") || !v[1].includes("expected_version")) throw new Error("verify payload wrong: " + (v && v[1]));
  const r = spawnSync(process.execPath, [fileURLToPath(import.meta.url)], {
    env: { ...process.env, SMOKE_NO_ME: "1" }, encoding: "utf8",
  });
  if (r.status !== 0) throw new Error("no-me run failed: " + r.stderr.slice(0, 300));
  console.log("smoke ok (본문 + no-me 서브)");
} else {
  await load(); await load(); await load();
  startMenu("A1");
  if (!els["#sheet"].innerHTML.includes("id=\"mein\"")) throw new Error("no-me: input missing");
  // 이름 없이도 agent 선택(step2)은 진행 가능 — dispatch author는 board
  await pickFromSheet("agent-1@host1");
  if (!els["#sheet"].innerHTML.includes("id=\"dmsg\"")) throw new Error("no-me: 이름 없이 step2 진행돼야 함");
  // 이름 없는 claim(내가 진행)은 차단
  startMenu("A1");
  if (!els["#sheet"].innerHTML.includes("datalist")) throw new Error("이름 히스토리 datalist 누락");
  els["#mein"] = els["#mein"] || { value: "" };
  els["#mein"].value = "";
  await pickFromSheet("__me__");
  if (!els["#toast"].textContent.includes("이름")) throw new Error("claim 가이드 toast 누락");
  if (store["tt-m-me"]) throw new Error("빈 이름이 저장됨");
  els["#mein"].value = "fresh@phone";
  await pickFromSheet("__me__");
  if (store["tt-m-me"] !== "fresh@phone") throw new Error("no-me: 이름 미저장");
  showDispatch("A1", "agent-1@host1");
  els["#dmsg"] = els["#dmsg"] || { value: "" };
  els["#dmsg"].value = "진행해줘";
  await sendDispatch("A1", "agent-1@host1");
  const last = calls.find(([u]) => u.includes("/dispatch"));
  if (!last || !last[1].includes("fresh@phone")) throw new Error("no-me: author 오류 " + (last && last[1]));
  console.log("no-me ok");
}
