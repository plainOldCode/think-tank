// mobile 코어 — 상수/상태/유틸/서명. 전역 배선(인라인 onclick) 때문에 classic script 유지.
const TABS = [["review","리뷰","rev"],["in_progress","진행중","prog"],["todo","시작 대기","todo"],["blocked","막힘","block"],["done","완료","done"]];
let LAST = "", tab = localStorage.getItem("tt-m-tab") || "review";
let OPEN = new Set(), DRAFT = {};
try { DRAFT = JSON.parse(localStorage.getItem("tt-m-draft") || "{}"); for (const id of JSON.parse(localStorage.getItem("tt-m-open") || "[]")) OPEN.add(id); } catch (e) { DRAFT = {}; }
let sub = localStorage.getItem("tt-m-sub") || "lease";
let AGENTS = [], SID = "";
let ME = localStorage.getItem("tt-m-me") || "";
const $ = s => document.querySelector(s);
const esc = s => String(s??"").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const hueOf = s => { let h = 0; for (const ch of String(s||"")) h = (h * 31 + ch.codePointAt(0)) % 360; return h; };
function tsDate(s){ const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})([+-]\d{2})(\d{2})$/.exec(s||""); if(!m) return null;
  const off = (+m[7])*60 + (m[7].startsWith("-") ? -(+m[8]) : +m[8]);
  return new Date(Date.UTC(+m[1], +m[2]-1, +m[3], +m[4], +m[5], +m[6]) - off*60000); }
function leaseLeft(i){ const t = tsDate(i.lease_expires); return t ? Math.round((t - Date.now())/60000) : null; }
function leaseTag(i){ const m = leaseLeft(i); if (m === null) return "";
  const alive = (m ?? -1) > 0;
  const dot = alive ? '<span class="hbdot"></span>' : "";
  const txt = m <= 0 ? "lease 만료" : m < 60 ? "잔여 "+m+"m" : "잔여 "+(m/60).toFixed(1)+"h";
  return `${dot}<span class="tag" style="color:${m <= 0 ? "var(--warn)" : "var(--prog)"}">${txt}</span>`; }
function setSub(v){ sub = v; localStorage.setItem("tt-m-sub",v); render(JSON.parse(LAST||"[]")); }
function toast(m){ const t=$("#toast"); t.textContent=m;
  const up = document.body.classList.contains("sheet-open");
  t.style.top = up ? "14%" : "auto"; t.style.bottom = up ? "auto" : "76px";
  t.classList.add("show"); clearTimeout(toast._h); toast._h = setTimeout(() => t.classList.remove("show"), 2600); }
function setMe(){ const v=prompt("작업자 이름(claim/verify 서명)", ME); if(v){ ME=v.trim(); localStorage.setItem("tt-m-me",ME); $("#me").textContent=ME; } return ME; }
function who(){ return ME; }
function saveMe(v){ ME=(v||"").trim(); if (ME){ localStorage.setItem("tt-m-me",ME); $("#me").textContent=ME;
    try{ const h=JSON.parse(localStorage.getItem("tt-m-names")||"[]").filter(n=>n!==ME); h.unshift(ME); localStorage.setItem("tt-m-names",JSON.stringify(h.slice(0,5))); }catch(e){} } return ME; }
function nameHist(){ try{ return JSON.parse(localStorage.getItem("tt-m-names")||"[]"); }catch(e){ return []; } }
function nameField(){ const h=nameHist(); const dl=h.map(n=>`<option value="${esc(n)}">`).join("");
  return `<input id="mein" list="mnames" placeholder="작업자 이름 (예: name@laptop) — claim/서명용" value="${esc(ME)}" style="width:100%;margin-top:8px;background:var(--bg);border:2px solid var(--line);border-radius:0;color:var(--tx);padding:12px;font-size:14.5px"><datalist id="mnames">${dl}</datalist>`; }
async function j(path, opts){ const r = await fetch(path, opts); const d = await r.json().catch(()=>({}));
  if(!r.ok) throw new Error(d.detail || r.status); return d; }
async function api(path, method, body){ try{ return await j(path,{method, headers:{"content-type":"application/json"}, body:JSON.stringify(body)}); }
  catch(e){ toast("⚠ " + (e.message||e)); } }
function act(fn){ return e => { e.stopPropagation(); fn(); }; }

// BUILD: 서버가 /m HTML meta(tt-build)에 주입하는 정적 자산 빌드 번호(mtime) — 불일치 시 자동 reload
const BUILD = +(document.querySelector('meta[name="tt-build"]')?.content || 0);
function collectDrafts(){ for (const id of OPEN){ const el = $("#ci-"+CSS.escape(id)); if (el && el.value) DRAFT[id] = el.value; }
  localStorage.setItem("tt-m-draft", JSON.stringify(DRAFT));
  localStorage.setItem("tt-m-open", JSON.stringify([...OPEN])); }
async function checkBuild(){
  try {
    const t = await (await fetch("/m", {cache:"no-store"})).text();
    const mm = t.match(/tt-build" content="(\d+)"/);
    if (mm && +mm[1] !== BUILD) { collectDrafts(); document.dispatchEvent(new CustomEvent("tt-reload")); location.reload(); }
  } catch (e) { }
}
setInterval(checkBuild, 300000);
