// 카드 상세 패널 — 상세 렌더/상태 이동/배정/댓글/하위 생성/dispatch
let current = null;

async function show(id, keep) {
  if (keep) {
    const ae = document.activeElement;
    if (ae && document.getElementById("detail").contains(ae)) return; // 타이핑 중 재렌더 금지
  }
  current = id;
  if (!keep) history.replaceState(null, "", "#" + id);
  const [i, tree, dsp] = await Promise.all([j("/issues/"+id), j("/issues/"+id+"/tree"), jQuiet("/issues/"+id+"/dispatches")]);
  const d = document.getElementById("dcontent");
  if (!keep) document.getElementById("detail").classList.add("open");
  const kids = (n) => n.tree.map(k => { const li = leaseInfo(k); return `<div class="child" style="${li ? "border-left:3px solid " + (li.stale ? "var(--warn)" : li.color) + ";" : ""}cursor:pointer" onclick="event.preventDefault();event.stopPropagation();show('${esc(k.id)}')"><span class="k">${esc(k.id)}</span> [${esc(k.state)}] ${li ? `<span style="color:${li.alive ? "var(--done)" : li.color};${li.alive ? "animation:hbpulse 1.1s infinite" : ""}">${li.stale ? "⌛" : li.alive ? "●" : "🔒"}</span> ` : ""}${esc(k.title)} ${k.assignee?"@"+esc(k.assignee):""}${k.tree.length?kids(k):""}</div>`; }).join("");
  const nextStates = {backlog:["todo","cancelled"], todo:["in_progress","blocked","backlog","cancelled"],
    in_progress:["blocked","done","todo","review"], review:["todo","blocked","done","cancelled"],
    blocked:["in_progress","todo","cancelled"], done:["todo"], cancelled:["todo"]};
  d.innerHTML = `
    ${i.parent_id ? `<p class="up"><button type="button" onclick="show('${esc(i.parent_id)}')">${TT_EN ? "parent issue" : "상위 이슈로"}</button></p>` : ""}
    <h2>${esc(i.title)}</h2>
    <div class="meta"><span class="k">${esc(i.id)}</span> · ${esc(i.state)} · ${i.priority?"p"+i.priority:"–"} · ${esc(i.labels.join(" "))||"—"} · ${i.assignee?("@"+esc(i.assignee)):"미배정"} · v${i.version}<br>
    created ${esc(i.created_at)}${i.completed_at?` · done ${esc(i.completed_at)}`:""}
    ${i.lease_by ? `<br>🔒 lease <b>${esc(i.lease_by)}</b> · 만료 ${esc(i.lease_expires?.slice(5,16))} · heartbeat ${esc(i.heartbeat_at?.slice(5,16) || "–")}` : ""}
    ${i.state==="blocked" && i.waiting_for ? `<br>⏸ waiting_for <b>${esc(i.waiting_for)}</b>${i.waiting_actor?` · 책임 ${esc(i.waiting_actor)}`:""}${i.blocked_detail?` · ${esc(i.blocked_detail)}`:""}${i.release_ready?` · <b style="color:var(--warn)">해제 가능</b>`:""}` : ""}</div>
    <pre>${esc(i.body || "(내용 없음)")}</pre>
    <div class="row"><span class="lab">이동:</span> ${nextStates[i.state].map(s=>`<button onclick="move('${esc(id)}','${s}')">${s}</button>`).join(" ")}
     ${i.state==="review" ? `· <button onclick="verifyCard('${esc(id)}')">✅ 사람 승인으로 완료</button><input id="vfee" placeholder="성공 결과(예: pytest 5 passed)" style="width:220px">` : ""}
     ${i.verified ? `· <span style="color:var(--done)">✓ ${{reported:"결과 보고",approved:"승인 예외",legacy:"기존 완료 기록"}[i.verification_status] || "기존 완료 기록"}${i.verified_evidence?` — ${esc(i.verified_evidence.slice(0,80))}`:""}</span>` : ""}
     ${["done","cancelled"].includes(i.state) ? (i.archived ? `· <button onclick="arch('${esc(id)}',false)">복원</button>` : `· <button onclick="arch('${esc(id)}',true)">아카이브</button>`) : ""}</div>
    <form class="row" onsubmit="return assign(event)">
      <span class="lab">배정:</span> <input name="agent" size="16" placeholder="예: opencode@thinkpad" value="${esc(i.assignee||"")}">
      <button>배정</button>
      ${i.assignee ? `<button type="button" onclick="return unassignNow()">해제</button>` : ""}
    </form>
    <div>${kids(tree)}</div>
    ${agents.some(a=>a.enabled) ? `
    <form class="row" onsubmit="return dispatchTo(event)">
      <select name="agent" style="color:var(--fg);background:var(--panel);border:1px solid var(--line)">${agents.filter(a=>a.enabled).map(a=>`<option value="${esc(a.name)}">${esc(a.name)}</option>`).join("")}</select>
      <input name="au" value="human" size="6" placeholder="지시자">
      <input name="msg" placeholder="agent에게 지시 (hook 발송)" style="flex:1" required>
      <button>지시</button>
    </form>` : `<p style="color:var(--dim);font-size:11px">등록 agent 없음 — 상단 Agents 버튼에서 webhook 등록</p>`}
    ${renderRunSection(dsp)}
    <h3 style="font-size:12px;color:var(--dim)">진행 로그 / 대화</h3>
    ${i.comments.map(c=>{const r=isResultComment(i,c);return `<div class="cmt${r?' result':''}"${r?' title="결과 보고"':''}><small>${esc(c.ts)} <span style="display:inline-block;width:8px;height:8px;border-radius:0;background:hsl(${hueOf(c.author)},70%,55%);margin:0 4px 0 2px;vertical-align:1px"></span><b style="color:hsl(${hueOf(c.author)},70%,65%)">${esc(c.author)}</b>${r?' <span style="color:var(--done)">결과</span>':''}</small><br>${esc(c.body)}</div>`;}).join("")||"<p style='color:var(--dim)'>없음</p>"}
    <form class="row" onsubmit="return cmt(event)">
      <input name="author" placeholder="agent" value="human" size="8" required>
      <input name="body" placeholder="로그 남기기" style="flex:1" required>
      <button>추가</button>
    </form>
    <form class="row" onsubmit="return child(event,'${esc(id)}')">
      <input name="title" placeholder="새 하위 이슈" style="flex:1" required>
      <button>하위 생성</button>
    </form>`;
}

function closeDetail() {
  document.getElementById("detail").classList.remove("open");
}

async function move(id, state) { await patch(id, {state}); load(); }
async function assign(e) { e.preventDefault(); const v = e.target.agent.value.trim(); if (v) await patch(current, {assignee: v}); load(); return false; }
async function unassignNow() { await patch(current, {assignee: ""}); load(); return false; }
async function arch(id, v) { await patch(id, {archived: v}); load(); }
function isResultComment(i, c) {
  if (/verification_status|completion_report|result:|rollup:|→\s*done|마감/.test(c.body)) return true;
  if (i.completed_at) { try { return tsParse(c.ts) >= tsParse(i.completed_at); } catch (e) {} }
  return false;
}

async function cmt(e) {
  e.preventDefault();
  const f = e.target;
  await post(`/issues/${current}/comments`, {author:f.author.value, body:f.body.value});
  f.body.value=""; show(current);
  return false;
}
async function child(e, parent) {
  e.preventDefault();
  await post("/issues", {title:e.target.title.value, parent_id:parent});
  e.target.title.value=""; load();
  return false;
}
async function dispatchTo(e) {
  e.preventDefault();
  const f = e.target;
  await post(`/issues/${current}/dispatch`, {agent:f.agent.value, message:f.msg.value, author:f.au.value||"board"});
  f.msg.value = "";
  show(current);
  return false;
}

async function newIssue(e) {
  e.preventDefault();
  const f = e.target;
  await post("/issues", {title:f.title.value, state:f.state.value});
  f.title.value=""; load();
  return false;
}
