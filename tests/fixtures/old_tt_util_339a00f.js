// 공용 유틸 — fetch 래퍼/이스케이프/시각 파싱. 전역 배선(인라인 onclick) 때문에 classic script 유지.
const API = "";
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
