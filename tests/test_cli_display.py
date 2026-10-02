"""S14R: done 유물 검증 표시 — list 배지, show 검증 행, search --verification 필터.

유물(done+unverified)은 현재 서버의 생성 경로가 없어(구 DB 이행분) tmp DB를
직접 갱신해 시뮬레이션한다 — 기존 데이터는 그대로 두고 표시 계층만 검증.
"""
import json
import pathlib
import sqlite3
import threading
import time
import urllib.request

import pytest
import uvicorn

from app import create_app

CLI = pathlib.Path(__file__).resolve().parents[1] / "cli" / "tt"


@pytest.fixture
def cli(tmp_path, monkeypatch):
    db = str(tmp_path / "cli.db")
    monkeypatch.setenv("TT_CONTRACT_VERSION", "2")
    app = create_app(db)
    sock = __import__("socket").socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True).start()
    deadline = time.monotonic() + 5
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started
    import os
    url = f"http://127.0.0.1:{sock.getsockname()[1]}"
    env = {**os.environ, "TT_URL": url, "TT_AGENT": "cli-test"}

    def run(*args):
        import subprocess
        return subprocess.run(["bash", str(CLI), *args], env=env,
                              capture_output=True, text=True, timeout=15)

    def api(path, method="GET", body=None):
        req = urllib.request.Request(url + path, method=method,
                                     data=json.dumps(body).encode() if body else None,
                                     headers={"content-type": "application/json",
                                              "x-agent": "cli-test"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.load(r) if method != "PATCH" else json.load(r)

    try:
        yield run, url, api, db
    finally:
        server.should_exit = True
        __import__("time").sleep(0.05)
        sock.close()


def new(run):
    return json.loads(run("new", "표시 테스트", "--json").stdout)["id"]


REPORT = {"method": "tdd",
          "design": {"criteria": "검증 표시", "verification": "본 테스트", "evidence": "수정 전 실패"},
          "implementation": {"summary": "cli 표시 계층", "commands": "edit cli/tt"},
          "verification": {"commands": "pytest tests/test_cli_display.py", "evidence": "통과"},
          "result": "passed", "limitations": ""}


def _done_reported(run, api, iid):
    run("claim", iid)
    ver = api(f"/issues/{iid}")["work_contract"]["version"]
    api(f"/issues/{iid}", "PATCH", {"state": "done",
                                    "completion_report": {**REPORT, "contract_version": ver,
                                                          "attempt": 1}})
    api(f"/issues/{iid}/verify", "POST", {"verifier": "cli-test",
                                          "completion_report": {**REPORT, "contract_version": ver,
                                                                "attempt": 1}})


def _make_artifact(db, iid):
    con = sqlite3.connect(db)
    # 진짜 유물: verified=0 + unverified (verified=1+unverified는 서버가 'legacy'로 표기)
    con.execute("UPDATE issues SET verification_status='unverified', verified=0 WHERE id=?", (iid,))
    con.commit()
    con.close()


def test_list_done_배지_unverified만(cli):
    run, url, api, db = cli
    kept = new(run)
    artifact = new(run)
    _done_reported(run, api, kept)
    _done_reported(run, api, artifact)
    _make_artifact(db, artifact)
    out = run("list", "done").stdout
    lines = {l.split()[0]: l for l in out.splitlines()}
    assert "⚠unverified" in lines[artifact]
    assert "⚠unverified" not in lines[kept]


def test_list_배지는_reported_승인카드에_없음(cli):
    run, url, api, db = cli
    iid = new(run)
    _done_reported(run, api, iid)
    assert "⚠unverified" not in run("list", "done").stdout
    assert "⚠legacy" not in run("list", "done").stdout


def test_show_done카드_검증_행(cli):
    run, url, api, db = cli
    artifact = new(run)
    _done_reported(run, api, artifact)
    _make_artifact(db, artifact)
    out = run("show", artifact).stdout
    assert "검증: unverified" in out


def test_search_verification_필터(cli):
    run, url, api, db = cli
    kept = new(run)
    artifact = new(run)
    for iid in (kept, artifact):
        _done_reported(run, api, iid)
    _make_artifact(db, artifact)
    run("list", "done")  # 워밍
    unv = run("search", "표시 테스트", "--verification", "unverified").stdout
    rep = run("search", "표시 테스트", "--verification", "reported").stdout
    assert artifact in unv and kept not in unv
    assert kept in rep and artifact not in rep
