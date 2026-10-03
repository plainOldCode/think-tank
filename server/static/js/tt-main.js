// 부팅 — 스모크 하니스가 이 두 줄을 정확히 치환하므로 서식 변경 금지
load().then(() => { if (location.hash) show(location.hash.slice(1)).catch(console.error); if (location.search.includes("agents")) toggleAgents(); }).catch(console.error);
setInterval(()=>load().catch(()=>{}), 5000);
