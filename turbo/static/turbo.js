function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[ch]));
}

function groupByState(items, order) {
  const groups = {};
  for (const name of order) groups[name] = [];
  const rest = [];
  for (const item of items) {
    const state = item.state || "unknown";
    if (groups[state]) groups[state].push(item);
    else rest.push(item);
  }
  return { groups, rest };
}

function actorName() {
  const el = document.getElementById("actor");
  return (el && el.value.trim()) || "human";
}

async function getJson(url, opts) {
  const res = await fetch(url, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(data.error || res.statusText);
    err.status = res.status;
    throw err;
  }
  return data;
}

function stuckHtml(items) {
  const rows = items || [];
  if (!rows.length) return "";
  return `<p class="glabel">stuck ${rows.length}</p>` + rows.map((item) =>
    `<article class="card" data-id="${esc(item.id)}" data-kind="turbo">
      <div class="id">stuck · ${esc(item.reason)} · ${esc(item.age_s)}s</div>
      <span class="ttl">${esc(item.title || item.id)}</span>
    </article>`
  ).join("");
}

function answerNote(card) {
  if (card.confirm) return "confirm: 기본값은 자동으로 적용되지 않는다.";
  if (card.answer) return "answer " + card.answer + " by " + card.answer_by;
  return "답이 없으면 기본값으로 넘어간다.";
}

function cardHtml(item) {
  const q = item.blocked_question ? `<div class="sub">${esc(item.blocked_question)}</div>` : "";
  return `<article class="card" data-id="${esc(item.id)}" data-kind="${esc(item.kind)}">
    <div class="id">${esc(item.state)} · ${esc(item.id)}</div>
    <span class="ttl">${esc(item.title || "(제목 없음)")}</span>
    ${q}
  </article>`;
}

function renderGroups(root, items, order) {
  const { groups, rest } = groupByState(items, order);
  const parts = [];
  for (const name of order) {
    const rows = groups[name];
    if (!rows.length) continue;
    parts.push(`<div class="group"><p class="glabel">${esc(name)} ${rows.length}</p>${
      rows.map(cardHtml).join("")}</div>`);
  }
  if (rest.length) {
    parts.push(`<div class="group"><p class="glabel">other ${rest.length}</p>${rest.map(cardHtml).join("")}</div>`);
  }
  root.innerHTML = parts.join("") || `<p class="note">없음</p>`;
}

async function load() {
  const archive = document.getElementById("archive");
  const turbo = document.getElementById("turbo");
  const banner = document.getElementById("banner");
  banner.textContent = "";
  try {
    const legacy = await getJson("/legacy/issues");
    const issues = (legacy.issues || []).map((item) => ({ ...item, kind: "legacy" }));
    renderGroups(archive, issues, ["backlog", "todo", "in_progress", "review", "blocked", "done", "cancelled"]);
  } catch (err) {
    archive.innerHTML = `<p class="note">${esc(err.message)}</p>`;
  }
  const stuckRoot = document.getElementById("stuck");
  if (stuckRoot) {
    try {
      const metrics = await getJson("/turbo/metrics");
      stuckRoot.innerHTML = stuckHtml(metrics.stuck || []);
    } catch (err) {
      stuckRoot.innerHTML = `<p class="err">${esc(err.message)}</p>`;
    }
  }
  try {
    const data = await getJson("/turbo/cards");
    const cards = (data.cards || []).map((item) => ({ ...item, kind: "turbo" }));
    renderGroups(turbo, cards, ["open", "working", "question", "awaiting-human", "submit", "review", "merged"]);
  } catch (err) {
    turbo.innerHTML = `<p class="err">${esc(err.message)}</p>`;
  }
}

function choiceButtons(card) {
  if (card.state !== "awaiting-human" || card.answer) return "";
  const options = card.blocked_options || [];
  if (!options.length) return "";
  return `<div class="choices">${options.map((opt) =>
    `<button type="button" data-choice="${esc(opt)}">${esc(opt)}${opt === card.blocked_default ? " · default" : ""}</button>`
  ).join("")}</div>`;
}

async function showLegacy(id) {
  const data = await getJson("/legacy/issues/" + encodeURIComponent(id));
  const issue = data.issue || {};
  const kids = (data.children || []).map((kid) =>
    `<div class="cmt"><button type="button" class="ghost" data-id="${esc(kid.id)}" data-kind="legacy">${esc(kid.id)}</button> [${esc(kid.state)}] ${esc(kid.title)}</div>`
  ).join("");
  const comments = (data.comments || []).map((c) =>
    `<div class="cmt"><small>${esc(c.ts)} ${esc(c.author)}</small><br>${esc(c.body)}</div>`
  ).join("") || `<p class="note">댓글 없음</p>`;
  const report = data.report ? `<pre>${esc(JSON.stringify(data.report, null, 2))}</pre>` : `<p class="note">보고 없음</p>`;
  const question = issue.blocked_question
    ? `<p>${esc(issue.blocked_question)}</p><p class="note">선택지 ${esc((issue.blocked_options || []).join(" / "))} · 기본값 ${esc(issue.blocked_default || "")}</p>`
    : "";
  document.getElementById("detail").innerHTML = `
    <p class="note">archive · read-only</p>
    <h3>${esc(issue.title)}</h3>
    <p class="meta">${esc(issue.id)} · ${esc(issue.state)} · ${esc(issue.assignee || "미배정")}</p>
    <pre>${esc(issue.body || "")}</pre>
    ${question}
    <h2>subtasks</h2>${kids || `<p class="note">없음</p>`}
    <h2>report</h2>${report}
    <h2>comments</h2>${comments}`;
}

async function showTurbo(id) {
  const card = await getJson("/turbo/cards/" + encodeURIComponent(id));
  const events = (card.events || []).map((ev) =>
    `<div class="cmt"><small>${esc(ev.ts)} ${esc(ev.kind)}</small></div>`
  ).join("");
  document.getElementById("detail").innerHTML = `
    <p class="note">turbo · ${esc(card.harness)} · v${esc(card.contract_version)}</p>
    <h3>${esc(card.title)}</h3>
    <p class="meta">${esc(card.id)} · ${esc(card.state)} · verify ${esc(card.verify_level || "—")} · verdict ${esc(card.verdict || "—")}</p>
    <pre>${esc(card.body || "")}</pre>
    <p>${esc(card.blocked_question || "")}</p>
    <p class="note">${esc(answerNote(card))}</p>
    ${choiceButtons(card)}
    <h2>events</h2>${events || `<p class="note">없음</p>`}`;
  document.getElementById("detail").dataset.card = id;
}

async function answer(id, choice) {
  const banner = document.getElementById("banner");
  try {
    await getJson("/turbo/cards/" + encodeURIComponent(id) + "/answer", {
      method: "POST",
      headers: {"content-type": "application/json"},
      body: JSON.stringify({actor: actorName(), choice}),
    });
    banner.textContent = "";
    await load();
    await showTurbo(id);
  } catch (err) {
    banner.textContent = err.message;
  }
}

function onClick(event) {
  const choice = event.target.closest("button[data-choice]");
  if (choice) {
    const id = document.getElementById("detail").dataset.card;
    if (id) answer(id, choice.getAttribute("data-choice"));
    return;
  }
  const card = event.target.closest("[data-id]");
  if (!card) return;
  const id = card.getAttribute("data-id");
  const kind = card.getAttribute("data-kind");
  const banner = document.getElementById("banner");
  const show = kind === "legacy" ? showLegacy(id) : showTurbo(id);
  show.catch((err) => { banner.textContent = err.message; });
}

function boot() {
  const reload = document.getElementById("reload");
  if (!reload) return;
  reload.addEventListener("click", () => load().catch((err) => {
    document.getElementById("banner").textContent = err.message;
  }));
  const actor = document.getElementById("actor");
  const saved = localStorage.getItem("turbo-actor");
  if (saved) actor.value = saved;
  actor.addEventListener("change", () => localStorage.setItem("turbo-actor", actor.value.trim()));
  document.body.addEventListener("click", onClick);
  load().catch((err) => { document.getElementById("banner").textContent = err.message; });
}

if (typeof document !== "undefined" && document.getElementById) boot();
