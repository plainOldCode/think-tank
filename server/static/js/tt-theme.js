// M43KDKWQ-6Y7Z: PR#40이 setTheme·closeDetail 호출부만 두고 정의를 빠뜨린 것을 메운 모듈.
// 테마는 data-theme 속성 + localStorage tt-theme로 지속. pixel은 기본값이라 속성을 두지 않음.
const THEMES = ["pixel", "ink", "newsprint", "terminal", "swiss", "ticket", "blueprint", "soft"];
function setTheme(name) {
  const root = document.documentElement;
  if (root && root.dataset) {
    if (!THEMES.includes(name)) name = "pixel";
    if (name === "pixel") delete root.dataset.theme;
    else root.dataset.theme = name;
  }
  try { localStorage.setItem("tt-theme", name); } catch (e) {}
  const sel = document.getElementById("theme");
  if (sel) sel.value = name;
}
function closeDetail() {
  const el = document.getElementById("detail");
  if (el && el.classList) el.classList.remove("open");
}
(() => {
  try {
    const saved = localStorage.getItem("tt-theme");
    if (saved && saved !== "pixel") setTheme(saved);
  } catch (e) {}
})();
