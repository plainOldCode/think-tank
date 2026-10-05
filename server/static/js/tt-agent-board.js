/* agent board — 에이전트 간 메시지 (M4580A48-573W). 자기완결 — tt-util에 의존하지 않는다. */
const abEsc = s => (s === null || s === undefined ? "" : String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;"));
const AB = {
  api: (p, method, body) => fetch(p, {
    method: method || "GET",
    headers: body ? { "content-type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  }).then(r => {
    if (!r.ok) return r.text().then(t => { throw new Error(`${r.status} ${t.slice(0, 120)}`); });
    return r.json();
  }),
  agents: [],
  viewer: localStorage.getItem("ab-viewer") || "",
  replyTo: null,
  lastTs: "",
};

function viewerName() { return document.getElementById("viewer").value || ""; }

async function loadAgents() {
  try {
    AB.agents = await AB.api("/agents");
  } catch { AB.agents = []; }
  const names = [...AB.agents.map(a => a.name), "skshim", "scott"];
  const opts = names.map(n => `<option value="${abEsc(n)}">${abEsc(n)}</option>`).join("");
  document.getElementById("author").innerHTML = opts;
  const viewer = document.getElementById("viewer");
  viewer.innerHTML = `<option value="">(시점 없음)</option>` +
    names.map(n => `<option${AB.viewer === n ? " selected" : ""} value="${abEsc(n)}">${abEsc(n)}</option>`).join("");
}

function renderBody(m) {
  const viewer = viewerName();
  let html = abEsc(m.body).replace(/@([^\s@,]+)/g, (s, name) => {
    const known = AB.agents.some(a => a.name === name);
    const hit = viewer && m.mentions.includes(`,${name},`);
    return `<span class="${known ? "mention" : ""}${hit && viewer === name ? " mine" : ""}">@${abEsc(name)}</span>`;
  }).replace(/\n/g, "<br>");
  return html;
}

function msgHtml(m, cls) {
  const viewer = viewerName();
  const unread = viewer && m.author !== viewer && !m.reads.includes(viewer);
  return `<div class="msg ${cls}" data-id="${abEsc(m.id)}">` +
    `<span class="who">${abEsc(m.author)}</span>` +
    (viewer && m.mentions.includes(`,${viewer},`) ? `<span class="badge">멘션</span>` : "") +
    (unread ? `<span class="badge">안읽음</span>` : "") +
    `<span class="when">${abEsc((m.created_at || "").slice(5, 16))}</span><br>` +
    `${renderBody(m)} ` +
    `<button class="glabel" onclick="reply('${abEsc(m.id)}', '${abEsc(m.thread_id || m.id)}')">답글</button>` +
    (viewer && unread ? ` <button class="glabel" onclick="markRead('${abEsc(m.id)}')">읽음</button>` : "") +
    (m.reads.length ? `<span class="when">읽음: ${m.reads.map(esc).join(", ")}</span>` : "") +
    `</div>`;
}

async function load() {
  const viewer = viewerName();
  localStorage.setItem("ab-viewer", viewer);
  const mentionOnly = document.getElementById("mentionme").checked && viewer;
  let url = `/messages?limit=200`;
  if (mentionOnly) url += `&mentions=${encodeURIComponent(viewer)}`;
  let ms;
  try { ms = await AB.api(url); } catch (e) {
    document.getElementById("board").innerHTML = `<div class="empty">${abEsc(String(e))}</div>`;
    return;
  }
  if (ms.length) AB.lastTs = ms[0].created_at;
  const byThread = new Map();
  for (const m of ms) {
    const t = m.thread_id || m.id;
    if (!byThread.has(t)) byThread.set(t, { root: null, replies: [] });
    const th = byThread.get(t);
    if (m.thread_id) th.replies.push(m);
    else th.root = m;
  }
  // codex F2: 목록 창(멘션 필터·limit) 밖의 루트 보완 — 답글만 있는 스레드가 사라지지 않게.
  const missing = [...byThread.entries()].filter(([, th]) => !th.root && th.replies.length).map(([t]) => t).slice(0, 20);
  for (const t of missing) {
    try {
      const full = await AB.api(`/messages?thread=${encodeURIComponent(t)}`);
      const root = full.find(m => !m.thread_id);
      if (root) byThread.get(t).root = root;
    } catch { /* 루트 소실 스레드는 답글만으로라도 렌더 */ }
  }
  const roots = [...byThread.values()].filter(th => th.root || th.replies.length)
    .sort((a, b) => b.root.created_at.localeCompare(a.root.created_at));
  document.getElementById("board").innerHTML = roots.length ? roots.map(th =>
    `<div class="thread"><div class="root">${msgHtml(th.root, "root")}</div>` +
    (th.replies.length ? `<div class="replies">${th.replies.map(r => msgHtml(r, "reply")).join("")}</div>` : "") +
    `</div>`).join("") : `<div class="empty">메시지가 없습니다 — 첫 공지를 올려보세요.</div>`;
  if (viewer) {
    const unread = await AB.api(`/messages/unread?agent=${encodeURIComponent(viewer)}`).catch(() => ({ count: 0 }));
    document.getElementById("unread").innerHTML = `안읽음 <b>${unread.count}</b>`;
  } else {
    document.getElementById("unread").innerHTML = "";
  }
}

function reply(id, threadRoot) {
  AB.replyTo = threadRoot;
  const el = document.getElementById("replyto");
  el.style.display = "";
  document.getElementById("body").focus();
}

function cancelReply() {
  AB.replyTo = null;
  document.getElementById("replyto").style.display = "none";
}

async function post() {
  const author = document.getElementById("author").value;
  const body = document.getElementById("body").value.trim();
  if (!body) return;
  const payload = { author, body };
  if (AB.replyTo) payload.thread_id = AB.replyTo;
  try {
    await AB.api("/messages", "POST", payload);
  } catch (e) { alert(e); return; }
  document.getElementById("body").value = "";
  cancelReply();
  await load();
}

async function markRead(id) {
  const viewer = viewerName();
  if (!viewer) return alert("먼저 시점을 선택하세요");
  try { await AB.api(`/messages/${id}/read`, "POST", { agent: viewer }); } catch (e) { return alert(e); }
  await load();
}

document.getElementById("post").onclick = post;
document.getElementById("body").addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); post(); }
});
loadAgents().then(load);
setInterval(() => { load(); }, 30000);
