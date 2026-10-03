// mobile 뷰 — 탭/목록/카드 렌더/상세 갱신
async function load(){
  let rows;
  try { [rows, AGENTS] = await Promise.all([j("/issues?archived=no"), j("/agents").catch(() => [])]); } catch(e){ toast("⚠ "+e.message); return; }
  $("#me").textContent = ME;
  $("#upd").textContent = new Date().toTimeString().slice(0,5);
  renderTabs(rows);
  const sig = JSON.stringify(rows);
  if (sig !== LAST) { LAST = sig; render(rows); }
}
async function refreshDetail(id){
  try {
    const i = await j("/issues/"+id);
    const d = $("#d-"+CSS.escape(id)); if(!d) return;
    d.querySelector(".body").textContent = i.body || "(본문 없음)";
    $("#cs-"+CSS.escape(id)).innerHTML = (i.comments||[]).slice(-4).map(c =>
      `<div class="cm"><b>${esc(c.author)} ${esc((c.ts||"").slice(5,16))}</b><br>${esc(c.body).slice(0,400)}</div>`).join("");
    const ci = $("#ci-"+CSS.escape(id)); if (ci && !ci.value && DRAFT[id]) ci.value = DRAFT[id];
  } catch(e){ toast("⚠ "+e.message); }
}
function renderTabs(rows){
  $("#tabs").innerHTML = TABS.map(([st,label]) => {
    const n = rows.filter(i => i.state === st).length;
    return `<button class="${tab===st?"on":""}" onclick="pick('${st}')"><span class="n">${n}</span>${label}</button>`;
  }).join("");
}
function pick(t){ tab=t; localStorage.setItem("tt-m-tab",t); renderTabs(JSON.parse(LAST||"[]")); render(JSON.parse(LAST||"[]")); }
function render(all){
  collectDrafts();
  const rows = (all||JSON.parse(LAST||"[]")).filter(i => i.state === tab)
    .sort((a,b) => (b.updated_at||"").localeCompare(a.updated_at||""));
  let html = "";
  if (tab === "in_progress") {
    const leased = rows.filter(r => (leaseLeft(r) ?? -1) > 0 || !!r.lease_by);
    const bar = `<div class="subtabs">
      <button class="${sub==="lease"?"on":""}" onclick="event.stopPropagation();setSub('lease')">청구중 ${leased.length}</button>
      <button class="${sub==="all"?"on":""}" onclick="event.stopPropagation();setSub('all')">전체 ${rows.length}</button></div>`;
    const show = sub === "lease" ? leased : rows;
    $("#list").innerHTML = bar + (show.length ? show.map(card).join("") : `<div class="empty">청구(lease) 중인 카드 없음</div>`);
    for (const id of OPEN) { const d = $("#d-"+CSS.escape(id)); if (d) { d.classList.add("open"); refreshDetail(id); } }
    return;
  }
  $("#list").innerHTML = rows.length ? rows.map(card).join("") : `<div class="empty">${TABS.find(t=>t[0]===tab)[1]} 없음</div>`;
  for (const id of OPEN) {
    const d = $("#d-"+CSS.escape(id));
    if (d) { d.classList.add("open"); refreshDetail(id); } else { OPEN.delete(id); delete DRAFT[id]; }
  }
}
function card(i){
  let acts = "", mine = false, other = false;
  if (i.state === "review") acts =
    `<button class="ok" onclick="pass('${esc(i.id)}',${i.version})">완료 처리</button>
     <button class="back" onclick="send('${esc(i.id)}','todo')">↩ 되돌림</button>`;
  if (i.state === "todo") acts =
    (i.assignee && i.assignee !== ME ? `<span class="tag">@${esc(i.assignee)}</span>`
     : `<button class="go" onclick="startMenu('${esc(i.id)}')">▶ 시작</button>`);
  if (i.state === "in_progress") {
    if (ME && (i.lease_by === ME || i.assignee === ME)) { mine = true; acts =
      `<button class="rev" onclick="send('${esc(i.id)}','review')">→ 리뷰 요청</button>
       <button class="hb" onclick="beat('${esc(i.id)}')">♥ 연장</button>`; }
    else { other = true; acts = `<span class="tag" style="color:var(--prog)">@${esc(i.assignee||i.lease_by||"?")} 진행중</span>`; }
  }
  if (i.state === "blocked") acts = `<span class="tag" style="color:var(--block)">wait: ${esc(i.waiting_actor||"?" )}</span>
    <button class="back" onclick="send('${esc(i.id)}','todo')">↩ 되돌림</button>`;
  if (i.state === "done") acts = i.verification_status ? `<span class="tag">${esc(i.verification_status)}</span>` : "";
  return `<div class="card${mine?" mine":""}${other?" other":""}" id="c-${esc(i.id)}">
    <span class="id">${esc(i.id)}</span> ${i.priority?`<span class="tag">p${i.priority}</span>`:""}${i.labels.map(l=>`<span class="tag">#${esc(l)}</span>`).join("")}
    <h3>${esc(i.title)}</h3>
    ${i.delayed?`<span class="tag" style="color:var(--warn)">🟡지연</span>`:""}${i.assignee?`<span class="tag">@${esc(i.assignee)}</span>`:""}${i.state==="in_progress" ? leaseTag(i) : (i.lease_expires?`<span class="tag">lease ${esc(i.lease_expires.slice(5,16))}</span>`:"")}
    <div class="acts">${acts}<button class="back" onclick="toggle('${esc(i.id)}')">자세히</button></div>
    <div class="det" id="d-${esc(i.id)}"><div class="body">불러오는 중…</div>
      <div id="cs-${esc(i.id)}"></div>
      <div class="addc"><input id="ci-${esc(i.id)}" placeholder="댓글 한 줄" enterkeyhint="send">
        <button onclick="comment('${esc(i.id)}')">↑</button></div></div>
  </div>`;
}
async function toggle(id){
  const d = $("#d-"+CSS.escape(id));
  d.classList.toggle("open");
  if (!d.classList.contains("open")) { OPEN.delete(id); delete DRAFT[id]; return; }
  OPEN.add(id);
  refreshDetail(id);
}
async function comment(id){
  const el = $("#ci-"+CSS.escape(id)); const b = el.value.trim(); if(!b) return;
  el.value = b;
  const me = who(); if(!me) return;
  if (await api("/issues/"+id+"/comments","POST",{author:me, body:b}) !== undefined){ el.value=""; delete DRAFT[id]; refreshDetail(id); toast("댓글 등록"); }
}
