"""BTRS(M40MP7K6) — probe 멀티워커·URL 고정 하드닝."""
import os
import threading
import time

import pytest

import importlib

from probe import core as probe_core


# --- URL 재평가: url=None이면 매 사이클 TT_URL을 다시 읽는다 ---

def test_url_없으면_매_사이클_env를_재평가한다(monkeypatch):
    seen = []

    def fake_run_once(url):
        seen.append(url)
        if len(seen) >= 2:
            stop.set()

    stop = threading.Event()
    monkeypatch.setattr(probe_core, "run_once", fake_run_once)
    _orig_sleep = probe_core._sleep
    monkeypatch.setattr(  # 파킹 인수계 폴링 모킹(P4) — 프로덕션 하한 5s 불변
        probe_core, "_sleep", lambda s, stop=None: _orig_sleep(min(s, 0.1), stop))
    monkeypatch.setenv("TT_URL", "http://first:7800")
    t = threading.Thread(target=probe_core.loop, args=(None, 0.01, stop), daemon=True)
    t.start()
    deadline = time.time() + 5
    while len(seen) < 1 and time.time() < deadline:
        time.sleep(0.01)
    monkeypatch.setenv("TT_URL", "http://second:7800")
    t.join(5)
    assert seen[0] == "http://first:7800"
    assert seen[-1] == "http://second:7800"  # 재시작 없이 URL 변경 반영


def test_명시_url은_고정된다(monkeypatch):
    seen = []

    def fake_run_once(url):
        seen.append(url)
        stop.set()

    stop = threading.Event()
    monkeypatch.setattr(probe_core, "run_once", fake_run_once)
    _orig_sleep = probe_core._sleep
    monkeypatch.setattr(  # 파킹 인수계 폴링 모킹(P4) — 프로덕션 하한 5s 불변
        probe_core, "_sleep", lambda s, stop=None: _orig_sleep(min(s, 0.1), stop))
    monkeypatch.setenv("TT_URL", "http://ignored:7800")
    t = threading.Thread(target=probe_core.loop, args=("http://fixed:7800", 0.01, stop),
                         daemon=True)
    t.start()
    t.join(5)
    assert seen == ["http://fixed:7800"]


# --- 멀티워커 가드: 파일 잠금 단일 구동 ---

def test_잠금은_단일_선점이고_해제되면_재선점된다(tmp_path):
    db = str(tmp_path / "t.db")
    fd1 = probe_core._acquire_probe_lock(db)
    assert fd1 is not None
    assert probe_core._acquire_probe_lock(db) is None  # 중복 선점 실패
    os.close(fd1)
    fd2 = probe_core._acquire_probe_lock(db)  # 리더 종료 → 인수계
    assert fd2 is not None
    os.close(fd2)


def test_잠금_보유_중_파킹하고_해제되면_인수계한다(tmp_path, monkeypatch):
    db = str(tmp_path / "t.db")
    leader_fd = probe_core._acquire_probe_lock(db)  # 리더가 선점한 상태
    assert leader_fd is not None

    ran = []
    stop = threading.Event()

    def fake_run_once(url):
        ran.append(url)
        stop.set()

    monkeypatch.setattr(probe_core, "run_once", fake_run_once)
    _orig_sleep = probe_core._sleep
    monkeypatch.setattr(  # 파킹 인수계 폴링 모킹(P4) — 프로덕션 하한 5s 불변
        probe_core, "_sleep", lambda s, stop=None: _orig_sleep(min(s, 0.1), stop))
    t = threading.Thread(target=probe_core.loop,
                         args=(None, 0.05, stop), kwargs={"db_path": db},
                         daemon=True)
    t.start()
    time.sleep(0.3)
    assert not ran  # 파킹 — 후보 사이클 실행 없음
    os.close(leader_fd)  # 리더 종료 — 잠금 해제
    t.join(10)
    assert ran  # 인수계 후 1회 실행


def test_잠금_획득_프로세스는_정상_구동된다(tmp_path, monkeypatch):
    db = str(tmp_path / "t.db")
    ran = []
    stop = threading.Event()

    def fake_run_once(url):
        ran.append(url)
        stop.set()

    monkeypatch.setattr(probe_core, "run_once", fake_run_once)
    _orig_sleep = probe_core._sleep
    monkeypatch.setattr(  # 파킹 인수계 폴링 모킹(P4) — 프로덕션 하한 5s 불변
        probe_core, "_sleep", lambda s, stop=None: _orig_sleep(min(s, 0.1), stop))
    t = threading.Thread(target=probe_core.loop,
                         args=(None, 0.05, stop), kwargs={"db_path": db},
                         daemon=True)
    t.start()
    t.join(10)
    assert ran and not t.is_alive()


# --- codex 리뷰: fd 누수·잠금 해제 ---

def test_선점_실패는_fd를_누수하지_않는다(tmp_path, monkeypatch):
    """반복 경합 후 잠금 fd가 닫혀 있어야 한다 — /proc 의존 없이 fstat EBADF로
    양 플랫폼(Linux/macOS CI)에서 검증한다."""
    db = str(tmp_path / "t.db")
    leader = probe_core._acquire_probe_lock(db)
    assert leader is not None
    opened = []
    real_open = os.open

    def spy_open(path, *a, **k):
        fd = real_open(path, *a, **k)
        if str(path).endswith(".probe.lock"):
            opened.append(fd)
        return fd

    monkeypatch.setattr(probe_core.os, "open", spy_open)
    try:
        for _ in range(32):
            assert probe_core._acquire_probe_lock(db) is None  # 반복 경합
    finally:
        monkeypatch.setattr(probe_core.os, "open", real_open)
    assert opened
    for fd in opened:
        with pytest.raises(OSError):
            os.fstat(fd)  # 닫혔어야 한다 — 누수면 유효
    os.close(leader)


def test_루프_종료_후_잠금이_해제된다(tmp_path, monkeypatch):
    """같은 프로세스에서 앱을 종료하고 다시 만들어도 후속 probe가 파킹하지 않는다."""
    db = str(tmp_path / "t.db")
    ran, stop = [], threading.Event()

    def fake_run_once(url):
        ran.append(url)
        stop.set()

    monkeypatch.setattr(probe_core, "run_once", fake_run_once)
    _orig_sleep = probe_core._sleep
    monkeypatch.setattr(  # 파킹 인수계 폴링 모킹(P4) — 프로덕션 하한 5s 불변
        probe_core, "_sleep", lambda s, stop=None: _orig_sleep(min(s, 0.1), stop))
    t = threading.Thread(target=probe_core.loop, args=(None, 0.05, stop),
                         kwargs={"db_path": db}, daemon=True)
    t.start()
    t.join(10)
    assert ran and not t.is_alive()
    fd = probe_core._acquire_probe_lock(db)  # 후속 앱 — 즉시 선점 가능
    assert fd is not None
    os.close(fd)  # 검증용 — 잠금을 남기지 않는다


def test_실제_앱_종료_재생성에서도_probe가_정상_구동된다(monkeypatch, tmp_path):
    """create_app/lifespan 실제 경로 — 앱1 종료 → 잠금 해제 → 앱2 probe 인수계."""
    from app import create_app
    from fastapi.testclient import TestClient
    db = str(tmp_path / "live.db")
    ran, stop_all = [], threading.Event()

    def fake_run_once(url):
        ran.append(url)

    monkeypatch.setattr(probe_core, "run_once", fake_run_once)
    _orig_sleep = probe_core._sleep
    monkeypatch.setattr(  # 파킹 인수계 폴링 모킹(P4) — 프로덕션 하한 5s 불변
        probe_core, "_sleep", lambda s, stop=None: _orig_sleep(min(s, 0.1), stop))
    monkeypatch.setenv("TT_PROBE_INTERVAL", "1")
    with TestClient(create_app(db)):
        deadline = time.time() + 5
        while not ran and time.time() < deadline:
            time.sleep(0.02)
        assert ran  # 앱1 probe 정상 구동
    # 앱1 종료(lifespan stop) — 스레드 종료+잠금 해제 대기
    deadline = time.time() + 5
    while time.time() < deadline:
        fd = probe_core._acquire_probe_lock(db)
        if fd is not None:
            os.close(fd)
            break
        time.sleep(0.02)
    else:
        pytest.fail("앱1 종료 후 잠금이 해제되지 않음")
    # 앱2 재생성 — 인수계로 정상 구동
    before = len(ran)
    with TestClient(create_app(db)):
        deadline = time.time() + 5
        while len(ran) <= before and time.time() < deadline:
            time.sleep(0.02)
        assert len(ran) > before, "앱2 probe가 구동되지 않음"
