// tmux 실행형 dispatch 진행 표시 (계약 M3EREF97 §2.3/§2.4)
// run_state 의미: ''=실행 정보 없음(미표시) · queued=회색 · running=blink(진행 관측 fresh)/aging(관측 오래됨)
// · stalled=적색(러너 stall_check 판정만 노출, 보드 재계산 금지) · finished=활성 소멸 · failed=적색(전달 status와 별개).
const RUN_BLINK_S = 90;          // running blink 임계: now-last_progress_at < 90s (계약 §2.4)
let ACTIVE_ROWS = null;          // GET /agents/active 수신 결과. null=아직 수신 전(미표시), []=활성 없음
let ACTIVE_ERR = false;          // 직전 조회 실패(마지막 표시 유지 + 실패 문구)

function runStateOfIssue(issueId) {
  const rows = (ACTIVE_ROWS || []).filter(a => a.issue_id === issueId && a.run_state);
  if (!rows.length) return null;
  return rows.reduce((best, r) => {
    const rank = { stalled: 3, running: 2, queued: 1 }[r.run_state] || 0;
    return rank > best.rank ? { row: r, rank } : best;
  }, { row: rows[0], rank: 0 }).row;
}
function chipFor(issueId) {
  const a = runStateOfIssue(issueId);
  if (!a) return "";                              // 실행 정보 없음 = 어떤 표시도 그리지 않는다
  const st = a.run_state;
  let cls = "run-chip run-" + st;
  let label = st;
  if (st === "running") {
    let fresh = false;
    try { fresh = a.last_progress_at && (Date.now() - tsParse(a.last_progress_at).getTime()) < RUN_BLINK_S * 1000; } catch (e) {}
    if (fresh) { cls += " blink"; } else { cls += " aging"; }   // 관측 오래됨 = blink 정지(진행률 아님)
  }
  const title = esc(`dispatch#${a.dispatch_id} · ${a.agent}${a.machine?" @"+a.machine:""}${a.session?" · "+a.session:""}`)
    + (a.last_progress_at ? ` · 마지막 진행 관측 ${esc(String(a.last_progress_at).slice(5,16))}` : "");
  return `<span class="${cls}" title="${title}">${esc(label)} <small>#${esc(String(a.dispatch_id))}</small></span>`;
}
async function refreshActive() {
  const rows = await jQuiet("/agents/active");
  const banner = document.getElementById("runbanner");
  if (rows === null) {                              // 조회 실패: 마지막 표시 유지 + 실패 문구(조용히, alert 없음)
    ACTIVE_ERR = true;
    if (ACTIVE_ROWS === null) { banner.innerHTML = "<span style=\"color:var(--warn)\">tmux 실행 상태 조회 실패</span>"; }
    else { banner.innerHTML += " <span style=\"color:var(--warn)\">· 상태 조회 실패(마지막 표시 유지)</span>"; }
    return;
  }
  ACTIVE_ERR = false;
  ACTIVE_ROWS = Array.isArray(rows) ? rows.filter(a => ["queued","running","stalled"].includes(a.run_state)) : [];
  renderBanner();
}
function renderBanner() {
  const banner = document.getElementById("runbanner");
  if (ACTIVE_ROWS === null) { banner.innerHTML = ""; return; }          // 초기 수신 전: 빈 상태(추측 표시 금지)
  if (!ACTIVE_ROWS.length) { banner.innerHTML = "tmux 실행 없음"; return; }
  const parts = ACTIVE_ROWS.map(a =>
    `<a href="#${esc(a.issue_id)}" style="color:var(--acc)" title="${esc(a.issue_title || "")}" onclick="event.preventDefault();show('${esc(a.issue_id)}')">#${esc(String(a.dispatch_id))} ${esc(a.issue_id.slice(-4))}·${chipFor(a.issue_id)}</a>`);
  banner.innerHTML = `실행(Active) ${ACTIVE_ROWS.length}: ${parts.join(" · ")}`;
}
function renderRunSection(dsp) {
  const rows = (dsp || []).filter(r => (r.run_state || "") !== "" || r.status === "error");
  if (!rows.length) return "";
  const line = r => {
    let chip = "";
    if (r.run_state) {
      const aging = r.run_state === "running" && r.last_progress_at &&
        (() => { try { return (Date.now() - tsParse(r.last_progress_at).getTime()) >= RUN_BLINK_S*1000; } catch (e) { return true; } })();
      chip = `<span class="run-chip run-${esc(r.run_state)}${r.run_state==="running"?(aging?" aging":" blink"):""}">${esc(r.run_state)}</span>`;
    }
    const tt = r.status === "error" ? ` <span style="color:#f85149" title="dispatch 웹훅 전달 실패(러너 실행 실패와 별개)">전달 error</span>` : "";
    return `<div class="cmt"><small>dispatch#${esc(String(r.id))} · ${esc(r.agent)} · ${chip || (r.status==="ok"?"전달 ok":"")}</small>${tt}${tt&&!chip?` <small style="color:var(--dim)">— 실행 수신 기록 없음(러너 진행 미표시)</small>`:""}`
      + `${r.machine ? `<br><small style="color:var(--dim)">${esc(r.machine)}${r.session?" · "+esc(r.session):""}</small>`:""}`
      + `${r.started_at ? `<br><small style="color:var(--dim)">시작 ${esc(String(r.started_at).slice(5,16))}</small>`:""}`
      + `${r.last_progress_at && (r.run_state==="running"||r.run_state==="stalled") ? `<br><small style="color:var(--dim)">마지막 진행 관측 ${esc(String(r.last_progress_at).slice(5,16))}</small>`:""}`
      + `${r.ended_at ? `<br><small style="color:var(--dim)">종료 ${esc(String(r.ended_at).slice(5,16))}</small>`:""}`
      + `${r.last_tail ? `<br><small style="color:var(--dim)">tail: ${esc(r.last_tail)}</small>`:""}</div>`;
  };
  return `<h3 style="font-size:12px;color:var(--dim)" title="러너(tt-runner)가 수신한 tmux 실행형 dispatch만 표시. run 상태(run)와 웹훅 전달 상태(status)는 별개의 층위입니다 — run failed ≠ 전달 실패.">tmux 실행 진행</h3>${rows.map(line).join("")}`;
}
