"""RQHD(M4W0VHZB) — 승인 경로 필드화(VQK1 B안): approval_path 컬럼.

probe 독립검증 done=independent-review / 증거만 done=evidence-only /
사람 승인=human / 보고 접수만(review 상태)=빈값 / done 이탈 시 소거.

RED 기준: 전부 구현 전 실패한다.
"""
import importlib
import json

import pytest
from fastapi.testclient import TestClient

import app as appmod


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("TT_REQUIRE_REPORT", "1")
    monkeypatch.setenv("TT_CONTRACT_VERSION", "2")
    m = importlib.reload(appmod)
    return TestClient(m.create_app(str(tmp_path / "tt.db")))


@pytest.fixture
def client_noreport(tmp_path, monkeypatch):
    # 증거 경로(ev/direct)는 보고 미강제 계약에서만 유효 — 계약이 create_app 시 고정되므로
    # 별도 앱으로 구성한다.
    monkeypatch.setenv("TT_REQUIRE_REPORT", "0")
    monkeypatch.setenv("TT_CONTRACT_VERSION", "2")
    m = importlib.reload(appmod)
    return TestClient(m.create_app(str(tmp_path / "tt.db")))


def _report(contract, attempt):
    return {
        "contract_version": contract, "attempt": attempt, "method": "planned",
        "result": "passed",
        "design": {"criteria": "c", "verification": "v"},
        "implementation": {"summary": "s", "commands": "cc"},
        "verification": {"commands": "ccc", "evidence": "1 passed"},
    }


def _submitted(client):
    issue = client.post("/issues", json={"acceptance": "완료 기준: 테스트 통과",
                                         "title": "승인 경로"}).json()
    issue = client.post(f"/issues/{issue['id']}/claim", json={"agent": "worker"}).json()
    rep = _report(issue["work_contract"]["version"], issue["execution_attempt"])
    r = client.patch(f"/issues/{issue['id']}",
                     json={"state": "done", "completion_report": rep})
    assert r.status_code == 200, r.text
    return client.get(f"/issues/{issue['id']}").json(), rep


def _approve(client, issue):
    base = f"/issues/{issue['id']}"
    assert client.post(base + "/claim-review", json={"agent": "codex"}).status_code == 200
    client.post(base + "/comments", json={
        "author": "codex", "body": f"review: approve\n{issue['review_subject']}\nlocal tests checked"})
    return client.get(base).json()


def test_probe_독립검증_done은_independent_review(client):
    issue, rep = _submitted(client)
    issue = _approve(client, issue)
    r = client.post(f"/issues/{issue['id']}/verify",
                    json={"verifier": "codex", "completion_report": rep,
                          "review_subject": issue["review_subject"]})
    assert r.status_code == 200 and r.json()["state"] == "done"
    got = client.get(f"/issues/{issue['id']}").json()
    assert got["approval_path"] == "independent-review"
    assert got["verification_status"] == "reported"


def test_증거만_verify는_evidence_only(client_noreport):
    # 증거 경로: gate 강등으로 review에 정지한 카드를 증거 verify로 done — 독립 승인 없음
    client = client_noreport
    issue = client.post("/issues", json={"acceptance": "완료 기준: 테스트 통과",
                                         "title": "증거만"}).json()
    issue = client.post(f"/issues/{issue['id']}/claim", json={"agent": "worker"}).json()
    r = client.patch(f"/issues/{issue['id']}", json={"state": "done"})
    assert r.status_code == 200 and r.json()["state"] == "review"
    r = client.post(f"/issues/{issue['id']}/verify",
                    json={"verifier": "codex", "evidence": "1 passed"})
    assert r.status_code == 200 and r.json()["state"] == "done"
    got = client.get(f"/issues/{issue['id']}").json()
    assert got["approval_path"] == "evidence-only"
    assert got["verification_status"] == "reported"


def test_사람_승인은_human(client):
    issue, _rep = _submitted(client)
    r = client.post(f"/issues/{issue['id']}/verify",
                    json={"verifier": "skshim", "human": True, "evidence": "사람 승인 사유"})
    assert r.status_code == 200 and r.json()["state"] == "done"
    got = client.get(f"/issues/{issue['id']}").json()
    assert got["approval_path"] == "human"
    assert got["verification_status"] == "approved"


def test_ev경로_직접_done도_evidence_only(client_noreport):
    client = client_noreport
    issue = client.post("/issues", json={"acceptance": "완료 기준: 테스트 통과",
                                         "title": "ev 경로"}).json()
    issue = client.post(f"/issues/{issue['id']}/claim", json={"agent": "worker"}).json()
    iid = issue["id"]
    client.post(f"/issues/{iid}/comments",
                json={"author": "w", "body": "result: 3 passed, 0 failed"})
    r = client.patch(f"/issues/{iid}", json={"state": "done"})
    assert r.status_code == 200 and r.json()["state"] == "done"
    got = client.get(f"/issues/{iid}").json()
    assert got["approval_path"] == "evidence-only"


def test_done_이탈시_소거(client):
    issue, rep = _submitted(client)
    issue = _approve(client, issue)
    r = client.post(f"/issues/{issue['id']}/verify",
                    json={"verifier": "codex", "completion_report": rep,
                          "review_subject": issue["review_subject"]})
    assert r.status_code == 200
    r2 = client.patch(f"/issues/{issue['id']}", json={"state": "todo"})
    assert r2.status_code == 200
    got = client.get(f"/issues/{issue['id']}").json()
    assert got["approval_path"] == ""


def test_보고_접수만은_빈값(client):
    issue, _rep = _submitted(client)
    got = client.get(f"/issues/{issue['id']}").json()
    assert got["state"] == "review"
    assert got["approval_path"] == ""


def test_기존_done_행은_빈값_유지(client, tmp_path):
    # 소급 0 — 컬럼 추가 이전 done 행을 시뮬레이션해도 빈값으로 판독된다.
    import sqlite3
    iid = client.post("/issues", json={"acceptance": "완료 기준: 테스트 통과",
                                       "title": "구데이터"}).json()["id"]
    con = sqlite3.connect(str(tmp_path / "tt.db"))
    con.execute("UPDATE issues SET state='done', verification_status='reported' WHERE id=?",
                (iid,))
    con.commit(); con.close()
    got = client.get(f"/issues/{iid}").json()
    assert got["state"] == "done" and got["verification_status"] == "reported"
    assert got["approval_path"] == ""
