// 보드 렌더 — 컬럼/카드/하위 트리/라벨 필터/lease 표시
const COLS = ["backlog","todo","in_progress","review","blocked","done"];
let SHOW_CX = localStorage.getItem("tt-show-cancelled") === "1";
function toggleCancelled() { SHOW_CX = !SHOW_CX; localStorage.setItem("tt-show-cancelled", SHOW_CX ? "1" : "0"); load(); }
let LAST = [];

const leaseInfo = i => {
  if (!i.lease_by) return i.state === "in_progress" ? { color:"#566068", text:"무표기 진행", stale:false } : null;
  const stale = i.lease_expires && tsParse(i.lease_expires) < new Date();
  const alive = !stale;
  const h = hueOf(i.lease_by);
  return { color: `hsl(${h},70%,55%)`, agent: i.lease_by, text: stale ? "⌛만료" : alive ? "●작업중" : "🔒" + i.lease_by, stale, alive };
};

async function load() {
  let issues = await j("/issues?limit=500&archived=" + (document.getElementById("arch").checked ? "all" : "no"));
  LAST = issues.slice();
  refreshAgents();
  await refreshActive();   // 조용한 조회(실패 시 마지막 표시 유지) — 실패해도 보드 렌더는 계속
  renderBoard(issues);
  if (current) show(current, true);
}

function renderBoard(issues) {
  const board = document.getElementById("board");
  board.innerHTML = "";
  const byId = Object.fromEntries(issues.map(i => [i.id, i]));
  const kidsOf = {};
  for (const i of issues) {
    if (i.parent_id && byId[i.parent_id]) (kidsOf[i.parent_id] ||= []).push(i);
  }
  const labSel = document.getElementById("lab");
  const want = labSel.value;
  const labels = [...new Set(issues.flatMap(i => i.labels))].sort();
  labSel.innerHTML = '<option value="">라벨 전체</option>' + labels.map(l => `<option${l===want?" selected":""} value="${esc(l)}">${esc(l)}</option>`).join("");
  if (!labSel.dataset.init) {
    labSel.dataset.init = "1";
    const u = new URLSearchParams(location.search).get("label");
    if (u && labels.includes(u)) { labSel.value = u; return load(); }
  }
  if (want && labels.includes(want)) {
    const keep = new Set();
    const addSub = id => { keep.add(id); (kidsOf[id] || []).forEach(k => addSub(k.id)); };
    for (const i of issues) if (i.labels.includes(want)) {
      addSub(i.id);
      for (let p = i.parent_id; p && byId[p]; p = byId[p].parent_id) keep.add(p);
    }
    issues = issues.filter(i => keep.has(i.id));
  }
  document.getElementById("counts").textContent = issues.length + " issues";
  const roots = issues.filter(i => !i.parent_id || !byId[i.parent_id]);
  const subRows = (parent) => (kidsOf[parent.id] || []).map(k => {
    const li = leaseInfo(k);
    const sty = li ? `cursor:pointer;border-left:3px solid ${li.stale ? "var(--warn)" : li.color};` : k.state === "done" ? "cursor:pointer;border-left:3px solid var(--done);" : "";
    return `<div class="sub${k.state === "done" ? " done" : ""}" style="${sty}" onclick="event.stopPropagation();show('${esc(k.id)}')">
       ${esc(k.id.slice(-4))} <span class="st">[${esc(k.state)}]</span>${li ? ` <span style="color:${li.alive ? "var(--done)" : li.color};${li.alive ? "animation:hbpulse 1.1s infinite" : ""}">${li.stale ? "⌛" : li.alive ? "●" : "🔒"}</span>` : ""} ${chipFor(k.id)} ${esc(k.title)}${k.assignee?` @${esc(k.assignee)}`:""}
       ${(kidsOf[k.id]||[]).length ? subRows(k) : ""}
     </div>`; });
  document.getElementById("cxbtn").style.opacity = SHOW_CX ? "1" : ".45";
  for (const st of (SHOW_CX ? COLS.concat("cancelled") : COLS)) {
    const rootsHere = roots.filter(x => x.state === st);
    const col = document.createElement("div"); col.className = "col";
    col.innerHTML = `<h3>${st} <span>${rootsHere.length}</span></h3>`;
    for (const i of rootsHere) {
      const li = leaseInfo(i);
      const c = document.createElement("div");
      c.className = "card" + (i.archived ? " archived" : "") + (li && li.stale ? " stale" : "") + (i.state === "done" ? " done" : "");
      if (li) c.style.borderLeft = `3px solid ${li.color}`;
      c.innerHTML = `<span class="id">${esc(i.id)}</span>
        ${i.archived ? `<span class="tag">🗄archived</span>` : ""}
        ${li ? `<span class="lease" style="color:${li.alive ? "var(--done)" : li.color};${li.alive ? "animation:hbpulse 1.1s infinite" : ""}"> ${esc(li.text)}</span>` : ""}
        ${i.labels.length?`<span class="tag"> ${esc(i.labels.join(" "))}</span>`:""}
        ${i.state==="blocked"&&i.waiting_for?`<span class="tag" style="color:var(--warn)">⏸ ${esc(i.waiting_for)}${i.waiting_actor?" @"+esc(i.waiting_actor):""}${i.release_ready?" →해제가능":""}</span>`:""}
        <p>${esc(i.title)}</p>${i.delayed?`<span class="tag" style="color:var(--warn)">🟡지연</span>`:""}${i.assignee?`<span class="tag">@${esc(i.assignee)}</span>`:""}
        ${(kidsOf[i.id]||[]).length ? `<div class="sub">${subRows(i)}</div>` : ""}`;
      c.onclick = () => show(i.id);
      col.appendChild(c);
    }
    board.appendChild(col);
  }
  if (current) show(current, true);
}
