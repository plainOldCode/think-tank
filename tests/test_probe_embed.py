"""probe 내장화 (M3ZW8E8A-ZK3G): 서버 프로세스 내 probe 루프, launchd 제거 전제.

- TT_PROBE_INTERVAL unset/0 → 스레드 없음(기본 off — 테스트·로컬 안전)
- TT_PROBE_INTERVAL>0 → 데몬 스레드 "tt-probe" 가동, shutdown에서 stop
- probe.loop(url, interval, stop) — run_once()를 주기 실행, 에러 백오프, stop 즉시 종료
- dispatchd.py는 shim: launchd 전환 기간 standalone 실행 + 기존 import 경로 유지
"""
import threading

import pytest
from fastapi.testclient import TestClient

from app import create_app


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("TT_PROBE_INTERVAL", raising=False)
    monkeypatch.delenv("TT_URL", raising=False)


def test_probe_thread_기본_비활성(tmp_path):
    app = create_app(str(tmp_path / "p.db"))
    with TestClient(app) as c:
        assert c.get("/health").status_code == 200
        assert not any(t.name == "tt-probe" for t in threading.enumerate())
        assert not hasattr(app.state, "probe_stop")


def test_probe_thread_interval설정시_가동_및_shutdown_정지(tmp_path, monkeypatch):
    import probe

    started = []
    monkeypatch.setenv("TT_PROBE_INTERVAL", "9999")
    monkeypatch.setattr(probe, "loop", lambda *a, **k: started.append(a))
    app = create_app(str(tmp_path / "p.db"))
    with TestClient(app) as c:
        c.get("/health")
    assert started, "probe 스레드가 start되어야 함"
    url, interval, stop = started[0]
    assert interval == 9999 and stop.is_set(), "shutdown에서 stop.set() 되어야 함"


def test_probe_loop_주기실행_및_stop_즉시종료(tmp_path, monkeypatch):
    import probe.core as core

    calls = []
    monkeypatch.setattr(core, "snapshot", lambda url: calls.append(url) or {"auto": False, "now": "", "agents": [], "issues": []})
    monkeypatch.setattr(core, "collect_prs", lambda repos: [])
    monkeypatch.setattr(core, "decide", lambda snap: [])

    stop = threading.Event()
    t = threading.Thread(target=core.loop, args=("http://self", 0.05, stop), daemon=True)
    t.start()
    deadline = threading.Event()
    for _ in range(100):
        if len(calls) >= 2:
            break
        deadline.wait(0.05)
    assert len(calls) >= 2, "loop가 주기마다 run_once를 실행해야 함"
    stop.set()
    t.join(timeout=5)
    assert not t.is_alive(), "stop 설정 후 loop는 즉시 종료"


def test_probe_loop_에러_백오프_후_계속(tmp_path, monkeypatch):
    import probe.core as core

    state = {"n": 0}

    def flaky(url):
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("server down")
        return {"auto": False, "now": "", "agents": [], "issues": []}

    monkeypatch.setattr(core, "snapshot", flaky)
    monkeypatch.setattr(core, "collect_prs", lambda repos: [])
    monkeypatch.setattr(core, "decide", lambda snap: [])

    stop = threading.Event()
    t = threading.Thread(target=core.loop, args=("http://self", 0.05, stop), daemon=True)
    t.start()
    deadline = threading.Event()
    for _ in range(100):
        if state["n"] >= 2:
            break
        deadline.wait(0.05)
    assert state["n"] >= 2, "1회 실패 후에도 루프는 계속되어야 함(백오프)"
    stop.set()
    t.join(timeout=5)
    assert not t.is_alive()


def test_dispatchd_shim_하위호환_리export():
    import dispatchd

    for name in ("decide", "execute", "snapshot", "collect_prs", "collect_repos", "ci_passed",
                 "card_repo", "run_once", "loop", "main", "api"):
        assert callable(getattr(dispatchd, name)), f"dispatchd.{name} 미노출"
