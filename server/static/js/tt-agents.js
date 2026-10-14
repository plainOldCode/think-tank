// agent 레지스트리 패널 — 라이브 점유 현황 + webhook 등록/토글/삭제
let agents = [];
async function refreshAgents() {
  try { agents = await j("/agents"); } catch (e) { agents = []; }
  document.getElementById("agentbtn").textContent = "Agents " + agents.filter(a=>a.enabled).length;
}

function toggleAgents() {
  const p = document.getElementById("agentpanel");
  p.style.display = p.style.display === "none" ? "block" : "none";
  if (p.style.display === "block") { renderAgents(); enrichLive(); renderRouting(); }
}

async function enrichLive() {
  const seen = new Set();
  for (const i of LAST) {
    const k = i.lease_by || i.assignee;
    if (!k || i.archived || i.state !== "in_progress" || seen.has(k)) continue;
    seen.add(k);
    try {
      const [d, dsp] = await Promise.all([j("/issues/"+i.id), j("/issues/"+i.id+"/dispatches").catch(()=>[])]);
      const mine = (d.comments||[]).filter(c=>c.author===k).slice(-1)[0];
      const last = (dsp||[]).slice(-1)[0];
      const el = document.getElementById("live-"+k);
      if (el && (mine || last))
        el.innerHTML += `<br>${last?`<small style="color:var(--dim)">dispatch#${last.id} ${esc(last.status)}</small><br>`:""}${mine?`<small>최신: ${esc(mine.body.slice(0,90))}</small>`:""}`;
    } catch (e) {}
  }
}

function renderAgents() {
  const p = document.getElementById("agentpanel");
  const cur = {}, legacy = [];
  for (const i of LAST) {
    const k = i.lease_by || i.assignee;
    if (!k || i.archived) continue;
    if (i.state !== "in_progress" && i.state !== "blocked") continue;
    if (!i.lease_by) { legacy.push(i); continue; }
    if (!cur[k] || (i.state === "in_progress" && cur[k].state !== "in_progress")) cur[k] = i;
  }
  const legacyRows = legacy.map(i => `<small style="color:var(--dim)">무lease 진행중 · ${esc(i.id)} ${esc(i.title.slice(0,40))} — @${esc(i.assignee)} (lease 체계 이전 카드)</small>`).join("<br>");
  const names = [...new Set([...agents.map(a=>a.name), ...Object.keys(cur)])].sort();
  const live = names.map(n => {
    const h = hueOf(n), i = cur[n];
    return `<div class="cmt" style="border-left-color:hsl(${h},70%,55%)"><b style="color:hsl(${h},70%,65%)">${esc(n)}</b>${agents.some(a=>a.name===n)?' <small style="color:var(--dim)">🔑등록</small>':''}<br>
      <small id="live-${esc(n)}">${i ? `현재 <a href="#${esc(i.id)}" style="color:var(--acc)" onclick="event.preventDefault();show('${esc(i.id)}')">${esc(i.id)} ${esc(i.title.slice(0,40))}</a> [${esc(i.state)}] ${i.lease_expires ? (tsParse(i.lease_expires) < new Date() ? '<span style="color:var(--warn)">⌛만료</span>' : 'lease '+esc(i.lease_expires.slice(5,16))) : "lease 없음"}` : "대기 중"}</small></div>`;
  }).join("") || "<p style='color:var(--dim)'>활성 agent 없음</p>";
  p.innerHTML = `<b>agents — 라이브</b>
    <div style="margin:4px 0 6px">${live}</div>
    ${legacyRows ? `<div style="margin:0 0 8px;padding:4px 8px;background:var(--panel);border-radius:0;border:2px solid var(--line)">${legacyRows}</div>` : ""}
    <hr style="border:none;border-top:2px solid var(--line);margin:10px 0">
    <b>등록 agent</b> <small style="color:var(--dim)">(webhook hook 대상)</small>
    ${agents.map(a=>`<div class="cmt">
      <small>${esc(a.name)} → <a href="${esc(a.base_url)}" style="color:var(--acc)" target="_blank" rel="noopener">${esc(a.base_url)}</a> ${a.secret?"🔑":""} ${a.enabled?'<span style="color:var(--done)">on</span>':'<span style="color:var(--warn)">off</span>'}</small><br>
      ${((a.model||a.reasoning||a.tier)?`<small style="color:var(--dim)">meta: ${a.model?("model="+esc(a.model)+(a.reasoning?"/"+esc(a.reasoning):"")):(a.reasoning?("reasoning="+esc(a.reasoning)):"")} ${a.tier?`<span style="color:var(--acc)">[${esc(a.tier)}]</span>`:""}</small><br>`:"")}
      <small style="color:var(--dim)">ok ${esc(a.last_ok?.slice(5,16)||"–")} · err ${esc(a.last_err||"–")}</small>
      <button data-agent="${esc(a.name)}" onclick="agentPatch(this.dataset.agent,{enabled:!${a.enabled?1:0}})">${a.enabled?"비활성":"활성"}</button>
      <button data-agent="${esc(a.name)}" onclick="delAgent(this.dataset.agent)">삭제</button>
    </div>`).join("") || "<p style='color:var(--dim)'>없음</p>"}
    <form onsubmit="return addAgent(event)" style="margin-top:8px">
      <input name="name" placeholder="agent명 (댓글 author와 일치)" required size="14">
      <input name="url" placeholder="http://host:port/hook" style="width:100%;margin-top:4px" required>
      <input name="secret" placeholder="secret(선택, Bearer)" style="width:100%;margin-top:4px">
      <button>등록</button>
    </form>
    <hr style="border:none;border-top:2px solid var(--line);margin:10px 0">
    <b>리뷰어 라우팅</b> <small style="color:var(--dim)">(폴백 순서·카운터파트 맵 — 저장 시 다음 probe 사이클부터 즉시 적용, 재배포 불필요)</small>
    <div id="routingbody"><small style="color:var(--dim)">불러오는 중…</small></div>`;
  renderRouting();
}

// 리뷰어 라우팅 설정(XV84) — 코드 기본값(DEFAULT_REVIEW_MAP·REVIEW_FALLBACK)을
// 저장 설정으로 덮는다. 순서는 쉼표 목록, 맵은 줄당 impl->reviewer.
async function renderRouting() {
  const el = document.getElementById("routingbody");
  if (!el) return;
  const d = await jQuiet("/settings/review-routing") || {};
  const fb = (d.review_fallback || []).join(", ");
  const mapLines = Object.entries(d.review_map || {}).map(([k,v]) => `${k}->${v}`).join("\n");
  el.innerHTML = `
    <small style="color:var(--dim)">현재: ${d.source === "stored" ? "저장 설정" : "코드 기본값"}${d.updated_at ? " · 수정 " + esc(d.updated_at.slice(5, 16)) : ""}</small>
    <form onsubmit="return saveRouting(event)" style="margin-top:4px">
      <input name="fallback" value="${esc(fb)}" style="width:100%" placeholder="폴백 순서(쉼표): agy, cursor-review, codex, claude" required>
      <textarea name="map" rows="3" style="width:100%;margin-top:4px" placeholder="카운터파트 맵(줄당 impl->reviewer, 비우면 기본):
codex->claude
claude->codex">${esc(mapLines)}</textarea>
      <button type="submit">저장</button>
      <button type="button" onclick="resetRouting()">기본값으로</button>
    </form>`;
}

async function saveRouting(e) {
  e.preventDefault();
  const f = e.target;
  const fb = f.fallback.value.split(",").map(s => s.trim()).filter(Boolean);
  if (!fb.length) { alert("폴백 순서는 최소 1명 이상"); return false; }
  const map = {};
  for (const line of f.map.value.split("\n")) {
    const t = line.trim(); if (!t) continue;
    const i = t.indexOf("->"); if (i < 1) continue;
    const k = t.slice(0, i).trim(), v = t.slice(i + 2).trim();
    if (k && v) map[k] = v;
  }
  await j("/settings/review-routing", {method: "PUT", headers: {"content-type": "application/json"}, body: JSON.stringify({review_fallback: fb, review_map: map})});
  await renderRouting();
  return false;
}

async function resetRouting() {
  if (!confirm("설정을 지우고 코드 기본값으로 복귀할까요?")) return;
  await j("/settings/review-routing", {method: "DELETE"});
  await renderRouting();
}

async function addAgent(e) {
  e.preventDefault();
  const f = e.target;
  await post("/agents", {name:f.name.value, base_url:f.url.value, secret:f.secret.value});
  await refreshAgents(); renderAgents();
  return false;
}
async function agentPatch(name, body) {
  await j("/agents/"+name, {method:"PATCH", headers:{"content-type":"application/json"}, body: JSON.stringify(body)});
  await refreshAgents(); renderAgents();
}
async function delAgent(name) {
  if (!confirm(name+" 삭제?")) return;
  await j("/agents/"+name, {method:"DELETE"});
  await refreshAgents(); renderAgents();
}
