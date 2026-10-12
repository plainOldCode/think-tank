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

    def fake_run_once(url, dry=False):
        seen.append(url)
        if len(seen) >= 2:
            stop.set()

    stop = threading.Event()
    monkeypatch.setattr(probe_core, "run_once", fake_run_once)
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

    def fake_run_once(url, dry=False):
        seen.append(url)
        stop.set()

    stop = threading.Event()
    monkeypatch.setattr(probe_core, "run_once", fake_run_once)
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

    def fake_run_once(url, dry=False):
        ran.append(url)
        stop.set()

    monkeypatch.setattr(probe_core, "run_once", fake_run_once)
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

    def fake_run_once(url, dry=False):
        ran.append(url)
        stop.set()

    monkeypatch.setattr(probe_core, "run_once", fake_run_once)
    t = threading.Thread(target=probe_core.loop,
                         args=(None, 0.05, stop), kwargs={"db_path": db},
                         daemon=True)
    t.start()
    t.join(10)
    assert ran and not t.is_alive()
