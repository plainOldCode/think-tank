"""CLI 재설치 게이트 스크립트 테스트 (JK8Z/M4TZHMST-JK8Z).

3케이스: 일치→no-op / 불일치→재설치+기록 갱신 / 파싱 실패→비정상 종료.
게이트는 실제 셸 스크립트라 subprocess로 실행하고, tt는 셈 shim으로 제어한다.
재설치 케이스만 실제 서버(uvicorn)의 /install.sh를 사용한다."""
import json
import os
import pathlib
import re
import subprocess
import threading
import time

import pytest
import uvicorn

from app import create_app

GATE = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "tt-reinstall-gate.sh"


@pytest.fixture
def gate_env(tmp_path, monkeypatch):
    """tmp HOME + tt shim 디렉터리. shim은 TT_SHIM_VERSION으로 출력을 제어한다."""
    home = tmp_path / "home"
    bin_dir = home / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    shim = bin_dir / "tt"
    shim.write_text('#!/bin/sh\ncat "$TT_SHIM_VERSION_FILE" 2>/dev/null\n')
    shim.chmod(0o755)
    return {"home": home, "bin": bin_dir, "shim": shim}


def _shim_output(text, env):
    f = env["home"] / "shim-version.txt"
    f.write_text(text)
    return str(f)


def _run_gate(env, extra_env=()):
    e = {**os.environ, **env}
    return subprocess.run(["bash", str(GATE)], env={**e, **dict(extra_env)},
                          capture_output=True, text=True, timeout=60)


def test_일치하면_noop(gate_env):
    # 기록 == 서버 해시 → no-op, 재설치 시도 없음(TT_URL을 닫힌 포트로 — 접촉하면 실패)
    v = "cli 2.5.3\nserver openapi 8a3a18af"
    e = _shim_output(v, gate_env)
    rec = gate_env["home"] / ".local" / "state" / "tt" / "installed-openapi"
    rec.parent.mkdir(parents=True)
    rec.write_text("8a3a18af")
    r = _run_gate(gate_env, {"TT_URL": "http://127.0.0.1:1", "TT_SHIM_VERSION_FILE": e})
    assert r.returncode == 0, r.stderr
    assert "no-op" in r.stdout
    assert rec.read_text() == "8a3a18af"


def test_불일치하면_재설치(gate_env):
    # 기록이 낡으면 서버 /install.sh로 재설치 → 새 tt의 version으로 재검증 → 기록 갱신
    # (shim은 실제 tt처럼 '라이브 서버의 현재 해시'를 출력한다 — 낡은 건 기록 파일 쪽)
    import hashlib
    import socket
    import urllib.request
    db = str(gate_env["home"] / "gate.db")
    app = create_app(db)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True).start()
    deadline = time.monotonic() + 5
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started
    try:
        url = f"http://127.0.0.1:{sock.getsockname()[1]}"
        body = urllib.request.urlopen(url + "/openapi.json", timeout=10).read()
        real_hash = hashlib.sha256(body).hexdigest()[:8]
        e = _shim_output(f"cli 2.5.3\nserver openapi {real_hash}", gate_env)
        r = _run_gate(gate_env, {"TT_URL": url, "TT_SHIM_VERSION_FILE": e})
        assert r.returncode == 0, r.stderr + r.stdout
        assert "reinstalled" in r.stdout
        # 실제 CLI가 설치되었고(shim 대체), 라이브 openapi 해시로 기록 갱신
        assert (gate_env["bin"] / "tt").exists()
        rec = gate_env["home"] / ".local" / "state" / "tt" / "installed-openapi"
        assert rec.read_text() == real_hash
    finally:
        server.should_exit = True
        time.sleep(0.05)
        sock.close()


def test_파싱실패는_비정상종료(gate_env):
    # tt version 출력이 파싱 불가 → 재설치하지 않고 exit 3, 기록·설치 무변경
    e = _shim_output("완전 엉터리 출력", gate_env)
    r = _run_gate(gate_env, {"TT_URL": "http://127.0.0.1:1", "TT_SHIM_VERSION_FILE": e})
    assert r.returncode == 3, r.stderr
    assert "FAIL" in r.stderr
    assert not (gate_env["bin"] / "tt").exists() or (gate_env["bin"] / "tt").stat().st_size < 200


def test_기록_부재는_재설치로_기준_수립(gate_env):
    # 기록 파일이 없으면 불일치로 간주 — 1회 재설치로 기준 수립(NCWZ 롤백 기준과 상통)
    e = _shim_output("cli 2.5.3\nserver openapi 8a3a18af", gate_env)
    r = _run_gate(gate_env, {"TT_URL": "http://127.0.0.1:1", "TT_SHIM_VERSION_FILE": e})
    # 서버에 접촉 실패 → 보수적으로 비정상 종료(재설치 시도는 했으나 fetch 실패)
    assert r.returncode == 3 and "fetch" in (r.stderr + r.stdout)
