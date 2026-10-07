/* 에이전트 보드 배지 (M4A55K92-4BZK) — index·m 공용. 자기완결 — 공용 스크립트 의존 없음. */
(function () {
  function refresh() {
    var el = document.getElementById("ab-badge");
    if (!el) return;
    var viewer = localStorage.getItem("ab-viewer") || "";
    if (!viewer) { el.style.display = "none"; return; }
    fetch("/messages/unread?agent=" + encodeURIComponent(viewer))
      .then(function (r) { return r.ok ? r.json() : { count: 0 }; })
      .then(function (d) {
        var n = (d && d.count) || 0;
        el.style.display = "";
        el.textContent = n > 99 ? "99+" : String(n);
        el.title = viewer + " 기준 안읽음 " + n;
      })
      .catch(function () { el.style.display = "none"; });
  }
  refresh();
  setInterval(refresh, 300000);
  window.abBadgeRefresh = refresh;
})();
