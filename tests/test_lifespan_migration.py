"""on_event(shutdown) → lifespan 마이그레이션 회귀 (TT M40MP7EX-RTZ1, kanban t_02337c75).

- 앱 생성 시 on_event deprecation 경고가 없어야 한다 — lifespan 컨텍스트로 교체됨
- 실제 앱 lifespan(startup+shutdown)을 실행하면 기존 종료 정리가 그대로 수행된다:
  probe stop.set(), notify 워커 refs 감소
- 레거시 on_startup/on_shutdown 핸들러 잔재가 없어야 한다
"""
import warnings

import pytest
from fastapi.testclient import TestClient

from app import _notify_workers, create_app


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("TT_PROBE_INTERVAL", raising=False)


def _on_event_warnings(caught):
    return [w for w in caught if "on_event is deprecated" in str(w.message)]


def test_앱_생성시_on_event_deprecation_경고_없음(tmp_path, monkeypatch):
    import probe

    # probe 스레드가 실제 네트워크 run_once를 하지 않도록 loop만 갈아끼움 (test_probe_embed 관례)
    monkeypatch.setenv("TT_PROBE_INTERVAL", "9999")
    monkeypatch.setattr(probe, "loop", lambda *a, **k: None)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        create_app(str(tmp_path / "p.db"))
    hits = _on_event_warnings(caught)
    assert not hits, f"on_event deprecation 경고 {len(hits)}건: {[str(w.message) for w in hits]}"


def test_lifespan_종료정리_probe_stop과_notify_refs_모두_수행(tmp_path, monkeypatch):
    import probe

    monkeypatch.setenv("TT_PROBE_INTERVAL", "9999")
    monkeypatch.setattr(probe, "loop", lambda *a, **k: None)
    path = str(tmp_path / "p.db")
    app = create_app(path)
    with TestClient(app):
        entry = _notify_workers[path]
        refs_before = entry["refs"]
        stop = app.state.probe_stop
        assert not stop.is_set()
    assert stop.is_set(), "lifespan 종료에서 probe stop.set() 되어야 함"
    assert entry["refs"] == refs_before - 1 or path not in _notify_workers, (
        "lifespan 종료에서 notify 워커 refs가 감소해야 함"
    )


def test_레거시_이벤트_핸들러_잔재_없음(tmp_path):
    app = create_app(str(tmp_path / "p.db"))
    assert app.router.on_startup == [], "레거시 on_startup 핸들러 잔재"
    assert app.router.on_shutdown == [], "레거시 on_shutdown 핸들러 잔재"
