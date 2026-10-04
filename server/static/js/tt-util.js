// 공용 유틸 — fetch 래퍼/이스케이프/시각 파싱. 전역 배선(인라인 onclick) 때문에 classic script 유지.
const API = "";
const TT_EN = (() => { try { return new URLSearchParams(location.search || "").get("lang") === "en"; } catch (e) { return false; } })();
function paintEnChrome() {
  if (!TT_EN) return;
  const put = (id, key, val) => { const el = document.getElementById(id); if (el) el[key] = val; };
  try { if (document.documentElement) document.documentElement.lang = "en"; } catch (e) {}
  put("newtitle", "placeholder", "New issue");
  put("newbtn", "textContent", "Add");
  put("cxbtn", "textContent", "Cancelled");
  put("cxbtn", "title", "Show or hide cancelled");
  put("reloadbtn", "textContent", "Refresh");
  put("dclose", "textContent", "✕ close");
  put("dpick", "textContent", "Select a card.");
  put("themelab", "textContent", "Theme");
  const themeSel = document.getElementById("theme");
  if (themeSel && themeSel.options) {
    const enName = { pixel: "Pixel", ink: "Ink", newsprint: "Newsprint", terminal: "Terminal", swiss: "Swiss", ticket: "Ticket", blueprint: "Blueprint", soft: "Soft" };
    for (let i = 0; i < themeSel.options.length; i++) {
      const opt = themeSel.options[i];
      if (enName[opt.value]) opt.textContent = enName[opt.value];
    }
    if (themeSel.setAttribute) themeSel.setAttribute("aria-label", "Theme");
  }
}
const TT_THEMES = ["pixel", "ink", "newsprint", "terminal", "swiss", "ticket", "blueprint", "soft"];
function applyTheme(name, persist) {
  if (!TT_THEMES.includes(name)) name = "pixel";
  try {
    const root = document.documentElement;
    if (root && root.setAttribute) {
      if (name === "pixel") root.removeAttribute("data-theme");
      else root.setAttribute("data-theme", name);
    }
  } catch (e) {}
  if (persist) { try { localStorage.setItem("tt-theme", name); } catch (e) {} }
  try {
    const sel = document.getElementById ? document.getElementById("theme") : null;
    if (sel) sel.value = name;
  } catch (e) {}
}
function setTheme(name) { applyTheme(name, true); }
function initTheme() {
  let name = "pixel";
  try { const s = localStorage.getItem("tt-theme"); if (TT_THEMES.includes(s)) name = s; } catch (e) {}
  applyTheme(name, false);
}
initTheme();
const esc = s => (s === null || s === undefined ? "" : String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;"));
const hueOf = s => { let h = 0; for (const c of s) h = (h * 31 + c.codePointAt(0)) % 360; return h; };
const tsParse = t => new Date(t.replace(/([+-]\d\d)(\d\d)$/, "$1:$2"));

async function j(path, opts) {
  const r = await fetch(API + path, opts);
  if (!r.ok) { alert((await r.json()).detail || r.status); throw new Error(r.status); }
  return r.json();
}
const jQuiet = async path => {   // 진행 조회 전용: 실패해도 alert/throw 없는 조용한 폴링
  try { const r = await fetch(API + path); if (!r.ok) throw new Error(r.status); return await r.json(); }
  catch (e) { return null; }
};
const post = (path, body) => j(path, {method:"POST", headers:{"content-type":"application/json"}, body: JSON.stringify(body)});
const patch = (id, body) => j("/issues/"+id, {method:"PATCH", headers:{"content-type":"application/json"}, body: JSON.stringify(body)});
