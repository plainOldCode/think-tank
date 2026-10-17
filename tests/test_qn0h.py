"""QN0H 결함 B — --print 드라이버 STALL 오탐 제거 (stall_mode=process)."""
import importlib.util
import time
import uuid
from pathlib import Path

import pytest


@pytest.fixture
def runner(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1] / "runner"
    monkeypatch.syspath_prepend(str(root))
    monkeypatch.setenv("TT_RUNNER_STATE", str(tmp_path / "runner"))
    monkeypatch.setenv("TT_RUNNER_SECRET", str(tmp_path / "secret"))
    monkeypatch.setenv("TT_RUNNER_REGISTRY", str(tmp_path / "registry.json"))
    monkeypatch.setenv("TT_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("TT_TMUX_SOCKET", "tt-qn0h-" + uuid.uuid4().hex)
    spec = importlib.util.spec_from_file_location("isolated_runner_qn0h", root / "tt-runner.py")
    r = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(r)
    r.ensure_dirs()
    monkeypatch.setattr(r, "tt_comment", lambda *a: None)
    monkeypatch.setattr(r, "tt_progress", lambda *a, **k: True)
    return r


def entry(r, agent="claude", status="running", started=None):
    e = {"issue_id": "TEST", "dispatch_id": 1, "session": "tt-test-1", "round": 1,
         "status": status, "started": started or time.time(), "agent": agent}
    r.save_runs({"TEST#1": e})
    return e


def profile(monkeypatch, r, **prof):
    monkeypatch.setattr(r, "resolve_profile",
                        lambda payload: dict(prof, binary=prof.get("binary", "/bin/echo")))


def test_process_mode_keeps_heartbeat_fresh_despite_silent_pane(runner, monkeypatch):
    """--print 드라이버(stall_mode=process): 세션 생존을 진행 신호로 — pane_ts 계속 갱신."""
    entry(runner)
    profile(monkeypatch, runner, driver="plain", stall_mode="process")
    monkeypatch.setattr(runner, "pane_fingerprint", lambda s, k: "fp-static")
    runner.heartbeat_pass()
    hb1 = runner.read_heartbeat("TEST#1")
    assert hb1["ok"] and hb1["pane_ts"] is not None
    time.sleep(0.01)
    runner.heartbeat_pass()
    hb2 = runner.read_heartbeat("TEST#1")
    assert hb2["pane_ts"] > hb1["pane_ts"], "동일 지문이어도 process 모드는 무음을 승계하지 않는다"


def test_process_mode_never_stalls_or_kills(runner, monkeypatch):
    entry(runner)
    profile(monkeypatch, runner, driver="plain", stall_mode="process")
    monkeypatch.setattr(runner, "pane_fingerprint", lambda s, k: "fp-static")
    runner.heartbeat_pass()
    comments = []
    monkeypatch.setattr(runner, "tt_comment", lambda i, b: comments.append(b))
    monkeypatch.setattr(runner, "session_work_state", lambda s: "busy", raising=False)
    assert runner.stall_check("TEST#1", runner.get_run("TEST#1"), time.time()) is False
    assert comments == []
    assert runner.get_run("TEST#1")["status"] == "running"


def test_pane_mode_still_accumulates_silence(runner, monkeypatch):
    """기본(stall_mode 미설정)은 기존 pane 관찰 — 회귀 방지."""
    entry(runner)
    profile(monkeypatch, runner, driver="plain")
    monkeypatch.setattr(runner, "pane_fingerprint", lambda s, k: "fp-static")
    runner.heartbeat_pass()
    hb1 = runner.read_heartbeat("TEST#1")
    old = time.time() - 700
    runner.write_heartbeat("TEST#1", dict(hb1, pane_ts=old))
    comments = []
    monkeypatch.setattr(runner, "tt_comment", lambda i, b: comments.append(b))
    monkeypatch.setattr(runner, "session_work_state", lambda s: "busy", raising=False)
    assert runner.stall_check("TEST#1", runner.get_run("TEST#1"), time.time()) is True
    assert runner.get_run("TEST#1")["status"] == "stalled"
    assert any("STALL" in c for c in comments)


def test_process_mode_session_gone_still_observed_as_failure(runner, monkeypatch):
    """세션 소실(process 모드라도)은 관찰 실패 경로 — ok=False, 판정 스킵."""
    entry(runner)
    profile(monkeypatch, runner, driver="plain", stall_mode="process")
    monkeypatch.setattr(runner, "pane_fingerprint", lambda s, k: None)
    runner.heartbeat_pass()
    hb = runner.read_heartbeat("TEST#1")
    assert hb["ok"] is False


def test_unresolvable_profile_falls_back_to_pane_mode(runner, monkeypatch):
    """프로필 미해석은 보수적으로 기존 pane 판정."""
    entry(runner)
    monkeypatch.setattr(runner, "resolve_profile", lambda payload: None)
    monkeypatch.setattr(runner, "pane_fingerprint", lambda s, k: "fp-static")
    runner.heartbeat_pass()
    hb1 = runner.read_heartbeat("TEST#1")
    time.sleep(0.01)
    runner.heartbeat_pass()
    hb2 = runner.read_heartbeat("TEST#1")
    assert hb2["pane_ts"] == hb1["pane_ts"], "기본 모드는 지문 불변 시 무음을 승계한다"


def test_process_mode_ignores_stale_heartbeat_beyond_threshold(runner, monkeypatch):
    """QN0H 리뷰 ④ — 관찰자 정지로 pane_ts가 임계를 넘어도 process 모드는 kill하지 않는다."""
    entry(runner)
    profile(monkeypatch, runner, driver="plain", stall_mode="process")
    monkeypatch.setattr(runner, "pane_fingerprint", lambda s, k: "fp-static")
    runner.heartbeat_pass()
    hb = runner.read_heartbeat("TEST#1")
    runner.write_heartbeat("TEST#1", dict(hb, pane_ts=time.time() - 700))
    comments = []
    monkeypatch.setattr(runner, "tt_comment", lambda i, b: comments.append(b))
    monkeypatch.setattr(runner, "session_work_state", lambda s: "busy", raising=False)
    assert runner.stall_check("TEST#1", runner.get_run("TEST#1"), time.time()) is False
    assert comments == [], "stale heartbeat라도 process 모드는 pane 무음 판정을 하지 않는다"
    assert runner.get_run("TEST#1")["status"] == "running"
