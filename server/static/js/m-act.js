// mobile 액션 — claim 시트/dispatch/상태 전이/사람 승인 verify
async function toggleKeep(id){ const d=$("#d-"+CSS.escape(id)); d.classList.remove("open"); refreshDetail(id); }
async function start(id){
  const me = who(); if(!me) return;
  await api("/issues/"+id,"PATCH",{assignee:me});
  if (await api("/issues/"+id+"/claim","POST",{agent:me})) toast("▶ 시작 — lease 3h");
  load();
}
function startMenu(id){
  SID = id;
  let html = `<h4>이 카드를 누가 진행할까요?</h4>`;
  if (!ME) html += nameField();
  const list = AGENTS.filter(a => a.enabled);
  html += `<button onclick="pickFromSheet('__me__')">내가 진행 (claim + lease 3h)</button>`;
  if (!list.length) html += `<button disabled>등록 agent 목록 로드 실패 — 새로고침 후 재시도</button>`;
  for (const a of list)
    html += `<button onclick="pickFromSheet('${esc(a.name)}')">${esc(a.name)}<small>${a.last_ok ? "✓ 마지막 수신 " + String(a.last_ok).slice(5,16) : "⚠ 수신 실패 기록"}</small></button>`;
  $("#sheet").innerHTML = html;
  $("#sheet").classList.add("open"); $("#scrim").classList.add("open");
  document.body.classList.add("sheet-open");
  if (!ME) $("#mein").focus();
}
function openSheet(html){
  $("#sheet").innerHTML = html;
  $("#sheet").classList.add("open"); $("#scrim").classList.add("open");
  document.body.classList.add("sheet-open");
}
async function pickFromSheet(aid){
  const id = SID;
  if (aid === "__me__") {
    if (!ME && !saveMe($("#mein").value)) {
      const el = $("#mein");
      if (el) { el.style.borderColor = "var(--block)"; el.focus(); }
      toast("이름을 입력한 뒤 진행해주세요 (agent 지시는 이름 없이 가능)");
      return;
    }
    closeSheet(); return start(id);
  }
  showDispatch(id, aid);
}
function showDispatch(id, aid){
  openSheet(`<h4>${esc(aid)} 에게 지시 (dispatch · 상태는 상대가 claim 시에만 이동)</h4>
    <textarea id="dmsg" rows="3" style="width:100%;margin-top:8px;background:var(--bg);border:2px solid var(--line);border-radius:0;color:var(--tx);padding:12px;font-size:14.5px">이 카드를 진행해주세요</textarea>
    <button class="go" onclick="sendDispatch('${esc(id)}','${esc(aid)}')">➤ dispatch 발송</button>
    <button class="back" style="margin-top:8px;width:100%" onclick="startMenu('${esc(id)}')">← 뒤로</button>`);
}
async function sendDispatch(id, aid){
  const msg = ($("#dmsg").value.trim()) || "이 카드를 진행해주세요";
  if (await api("/issues/"+id+"/dispatch","POST",{agent:aid, message:msg, author:who() || "board"})) { toast("➤ "+aid+" dispatch — 회신은 댓글"); closeSheet(); }
}
function closeSheet(){ $("#sheet").classList.remove("open"); $("#scrim").classList.remove("open"); document.body.classList.remove("sheet-open"); }
async function send(id, state){
  if (await api("/issues/"+id,"PATCH",{state})) load();
}
async function beat(id){
  const me = who(); if(!me) return;
  if (await api("/issues/"+id+"/lease","POST",{agent:me})) toast("♥ lease 연장");
}
async function pass(id, version){
  SID = id;
  let html = `<h4>완료 통과 근거 한 줄 (서버 저장·감사용)</h4>`;
  if (!ME) html += nameField();
  html += `<input id="pev" placeholder="예: 재실행 통과, 스모크 green" style="width:100%;margin-top:8px;background:var(--bg);border:2px solid var(--line);border-radius:0;color:var(--tx);padding:12px;font-size:14.5px">
    <button class="ok" onclick="passConfirm(${version})">✓ done 처리</button>
    <button class="back" style="margin-top:8px;width:100%" onclick="closeSheet()">취소</button>`;
  openSheet(html);
}
async function passConfirm(version){
  if (!ME && !saveMe($("#mein").value)) {
    const el = $("#mein");
    if (el) { el.style.borderColor = "var(--block)"; el.focus(); }
    toast("위 작업자 이름을 먼저 입력한 뒤 눌러주세요");
    return;
  }
  const ev = ($("#pev").value.trim()) || "mobile pass";
  if (await api("/issues/"+SID+"/verify","POST",{verifier:who(), evidence:ev, expected_version:version, human:true}) !== undefined) { toast("\u2713 done(사람 승인)"); closeSheet(); }
  load();
}
