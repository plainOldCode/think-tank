// 사람 승인 verify (약한 합의 — THNJ): 자기선언 서명, 감사 기록 전용
function signerName() {
  let v = localStorage.getItem("tt-signer");
  if (!v) { v = prompt("검증자 이름(사람 승인 감사 기록)", "skshim") || "skshim"; localStorage.setItem("tt-signer", v); }
  return v;
}
async function verifyCard(id) {
  const ev = (document.getElementById("vfee")?.value || "").trim();
  if (!ev) { alert("검증 노트 한 줄을 입력하세요"); return; }
  await post(`/issues/${id}/verify`, {verifier: signerName(), evidence: ev, human: true});
  load();
}
