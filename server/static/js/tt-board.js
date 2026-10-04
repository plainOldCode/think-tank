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


function ssGet(k) { try { return sessionStorage.getItem(k); } catch (e) { return null; } }
function ssSet(k, v) { try { sessionStorage.setItem(k, v); } catch (e) {} }
function groupOpen(key) { return ssGet("tt-grp-" + key) !== "0"; }
function nodeOpen(id) { return ssGet("tt-node-" + id) !== "0"; }
function groupOf(i) {
  if (!i) return "";
  if (i.state === "review") return "review";
  if (i.state === "in_progress") return "progress";
  if (i.state === "todo" || i.state === "backlog") return "wait";
  if (i.state === "blocked") return "block";
  if (i.state === "done") return "done";
  if (i.state === "cancelled") return "cancel";
  return "";
}
function toggleGroup(g, key) {
  if (!g || !g.classList) return;
  g.classList.toggle("shut");
  ssSet("tt-grp-" + key, g.classList.contains("shut") ? "0" : "1");
}
function toggleNode(id, btn) {
  const node = btn && btn.closest ? btn.closest(".node") : null;
  if (!node || !node.classList) return;
  node.classList.toggle("shut");
  ssSet("tt-node-" + id, node.classList.contains("shut") ? "0" : "1");
}

async function load() {
  let issues = await j("/issues?limit=500&archived=" + (document.getElementById("arch").checked ? "all" : "no"));
  LAST = issues.slice();
  refreshAgents();
  await refreshActive();   // 조용한 조회(실패 시 마지막 표시 유지) — 실패해도 보드 렌더는 계속
  renderBoard(issues);
  if (current) show(current, true);
}


function subtreeCount(id, kidsOf) {
  let done = 0, total = 0;
  for (const k of kidsOf[id] || []) {
    const sub = subtreeCount(k.id, kidsOf);
    if (k.state !== "cancelled") {
      total += 1;
      if (k.state === "done") done += 1;
    }
    done += sub.done;
    total += sub.total;
  }
  return { done, total };
}
function subtreeBar(id, kidsOf) {
  const c = subtreeCount(id, kidsOf);
  if (!c.total) return "";
  const w = Math.round(100 * c.done / c.total);
  return `<div class="subprog"><i><b style="width:${w}%"></b></i><em>${c.done}/${c.total}</em></div>`;
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
  labSel.innerHTML = `<option value="">${TT_EN ? "All labels" : "라벨 전체"}</option>` + labels.map(l => `<option${l===want?" selected":""} value="${esc(l)}">${esc(l)}</option>`).join("");
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
  const leaseRemain = (i) => {
    if (!i.lease_expires) return "";
    let t; try { t = tsParse(i.lease_expires); } catch (e) { return ""; }
    if (!t || isNaN(+t)) return "";
    const m = Math.round((t - Date.now()) / 60000);
    if (m <= 0) return "lease 만료";
    if (m < 60) return "잔여 " + m + "m";
    return "잔여 " + (m / 60).toFixed(1) + "h";
  };
  const agentLine = (i) => {
    const name = i.assignee || "";
    const remain = leaseRemain(i);
    if (!name && !remain) return "";
    const dot = name ? `<i class="seal" title="${esc(name)}" style="background:hsl(${hueOf(name)},70%,46%)"></i>` : "";
    return `<div class="who">${dot}${name ? "@"+esc(name) : ""}${remain ? ` <span class="lease">${esc(remain)}</span>` : ""}</div>`;
  };
  const stateLine = (i) => {
    const bits = [];
    const progWord = TT_EN ? "In progress" : "진행중";
    if (i.state === "in_progress") bits.push(i.lease_by && i.lease_by !== i.assignee ? `@${esc(i.lease_by)} ${progWord}` : progWord);
    else if (i.state === "blocked") bits.push(`wait: ${esc(i.waiting_actor || "?")}` + (i.waiting_for ? ` · ${esc(i.waiting_for)}${i.release_ready ? " →해제가능" : ""}` : ""));
    else if (i.state === "done" && i.verification_status) bits.push(esc(i.verification_status));
    if (i.delayed) bits.push("🟡지연");
    if (i.archived) bits.push("🗄archived");
    const chip = chipFor(i.id);
    if (chip) bits.push(chip);
    return bits.length ? `<div class="subline">${bits.join(" · ")}</div>` : "";
  };
  const byUpdated = (a, b) => (b.updated_at || "").localeCompare(a.updated_at || "");
  const renderNode = (i) => {
    const kids = (kidsOf[i.id] || []).filter(k => groupOf(k) === groupOf(i)).sort(byUpdated);
    const shut = kids.length && !nodeOpen(i.id);
    const node = document.createElement("div");
    node.className = "node" + (shut ? " shut" : "");
    const li = leaseInfo(i);
    const parent = i.parent_id ? byId[i.parent_id] : null;
    const cross = i.parent_id && (!parent || groupOf(parent) !== groupOf(i));
    const twist = kids.length ? `<button type="button" class="twist" onclick="event.stopPropagation();toggleNode('${esc(i.id)}',this)"><span class="when-open">▾</span><span class="when-shut">▸</span></button>` : "";
    const card = document.createElement("div");
    card.className = "card" + (i.archived ? " archived" : "") + (li && li.stale ? " stale" : "") + (i.state === "done" ? " done" : "");
    const bar = subtreeBar(i.id, kidsOf);
    card.innerHTML = `<div class="idline">${twist}<span class="id">${esc(i.id)}</span>${i.priority?` <span class="tag">p${esc(i.priority)}</span>`:""}${(i.labels||[]).map(l=>`<span class="tag">#${esc(l)}</span>`).join("")}${cross?` <span class="parentref">↑ ${esc(parent ? parent.id : i.parent_id)}</span>`:""}</div>
      <div class="ttl">${esc(i.title)}</div>
      ${agentLine(i)}
      ${stateLine(i)}
      ${bar}`;
    card.onclick = () => show(i.id);
    node.appendChild(card);
    if (kids.length) {
      const box = document.createElement("div");
      box.className = "kids";
      for (const k of kids) box.appendChild(renderNode(k));
      node.appendChild(box);
    }
    return node;
  };
  document.getElementById("cxbtn").style.opacity = SHOW_CX ? "1" : ".45";
  const groups = TT_EN
    ? [["review","Review"],["progress","In progress"],["wait","Waiting"],["block","Blocked"],["done","Done"]]
    : [["review","리뷰"],["progress","진행중"],["wait","시작 대기"],["block","막힘"],["done","완료"]];
  if (SHOW_CX) groups.push(["cancel", TT_EN ? "Cancelled" : "취소됨"]);
  const list = document.createElement("div"); list.className = "list";
  for (const [key, label] of groups) {
    const members = issues.filter(x => groupOf(x) === key);
    if (!members.length) continue;
    const tops = members.filter(i => {
      const parent = i.parent_id && byId[i.parent_id];
      return !parent || groupOf(parent) !== key;
    }).sort(byUpdated);
    const open = groupOpen(key);
    const g = document.createElement("div");
    g.className = "group" + (open ? "" : " shut");
    const h = document.createElement("button");
    h.type = "button";
    h.className = "glabel";
    h.innerHTML = `<span class="twist"><span class="when-open">▾</span><span class="when-shut">▸</span></span> ${label} <span class="n">${members.length}</span>`;
    h.onclick = () => toggleGroup(g, key);
    const body = document.createElement("div");
    body.className = "gbody";
    for (const i of tops) body.appendChild(renderNode(i));
    g.appendChild(h);
    g.appendChild(body);
    list.appendChild(g);
  }
  board.appendChild(list);
  board.appendChild(list);
  if (current) show(current, true);
}

paintEnChrome();
