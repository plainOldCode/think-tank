"""R7Y4(M53PBDC0-R7Y4) — DX88 후속: 별칭 정규화 자기승인 우회·리뷰 자가수령·
heartbeat 409·마이그레이션 reviewer/nominee 누락.

재현 시나리오: 카드는 별칭 등록 전에 원래 이름("worker")으로 수령됨(저장값 비정규화).
이후 별칭 "worker" → 등록명 "worker@t" 등록. 저장값이 비정규화인 채로
- verify를 등록명("worker@t")으로 → 자기승인 가드 미작동(200, 409여야 함)
- claim-review를 등록명으로 → 본인 작업 수령 성공(409여야 함)
- heartbeat/ping을 등록명으로 → 정당 보유자인데 409
"""
import importlib

import pytest
from fastapi.testclient import TestClient

import app as appmod


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # v2 계약 — 제출은 review 상태 전이(보고 필수). 자기승인·자가수령 가드는 v1·v2 공통이지만
    # 테스트 재현은 v2 제출 경로가 단순하다.
    monkeypatch.setenv("TT_REQUIRE_REPORT", "1")
    monkeypatch.setenv("TT_CONTRACT_VERSION", "2")
    m = importlib.reload(appmod)
    return TestClient(m.create_app(str(tmp_path / "tt.db")))


@pytest.fixture()
def iid(client):
    return client.post("/issues", json={"title": "자기승인 우회 재현", "type": "task",
                                        "acceptance": "완료 기준: 테스트 통과"}).json()["id"]


def _to_review(client, iid):
    issue = client.get(f"/issues/{iid}").json()
    rep = {"contract_version": issue["work_contract"]["version"],
           "attempt": issue["execution_attempt"], "method": "planned",
           "result": "passed",
           "design": {"criteria": "c", "verification": "v"},
           "implementation": {"summary": "s", "commands": "cc"},
           "verification": {"commands": "ccc", "evidence": "1 passed"}}
    r = client.patch(f"/issues/{iid}", json={"state": "done", "completion_report": rep})
    assert r.status_code == 200, r.text


@pytest.fixture()
def _late_alias(client, iid):
    """카드를 비정규화 이름 'worker'로 claim 후, 별칭 'worker'→'worker@t'를 나중에 등록.

    제출(review 전이)은 lease를 해제하므로 heartbeat 재현은 이 fixture만 쓴다."""
    r = client.post(f"/issues/{iid}/claim", json={"agent": "worker", "hours": 2})
    assert r.status_code == 200, r.text
    r = client.post("/agents", json={"name": "worker@t", "base_url": "http://127.0.0.1:1",
                                     "aliases": ["worker"]})
    assert r.status_code in (200, 201), r.text


@pytest.fixture()
def _late_alias_submitted(client, iid, _late_alias):
    _to_review(client, iid)


def test_verify_등록명으로_자기승인_차단(client, iid, _late_alias_submitted):
    r = client.post(f"/issues/{iid}/verify", json={"verifier": "worker@t"})
    assert r.status_code == 409, r.text
    assert "cross-review" in r.json()["detail"]


def test_claim_review_등록명으로_본인작업_수령_차단(client, iid, _late_alias_submitted):
    r = client.post(f"/issues/{iid}/claim-review", json={"agent": "worker@t"})
    assert r.status_code == 409, r.text
    assert "cross-review" in r.json()["detail"]


def test_heartbeat_정당보유자_별칭등록명_수용(client, iid, _late_alias):
    r = client.post(f"/issues/{iid}/lease", json={"agent": "worker@t", "hours": 2})
    assert r.status_code == 200, r.text
    r = client.post(f"/issues/{iid}/ping", json={"agent": "worker@t"})
    assert r.status_code == 200, r.text


def test_마이그레이션에_reviewer_nominee_포함(client, iid, tmp_path):
    import sqlite3

    from migrations import migrate_assignees

    client.post(f"/issues/{iid}/claim", json={"agent": "worker", "hours": 2})
    client.post("/agents", json={"name": "worker@t", "base_url": "http://127.0.0.1:1",
                                 "aliases": ["worker"]})
    _to_review(client, iid)
    r = client.post(f"/issues/{iid}/claim-review", json={"agent": "other"})
    assert r.status_code == 200, r.text
    con = sqlite3.connect(tmp_path / "tt.db")
    con.row_factory = sqlite3.Row
    con.execute("UPDATE issues SET reviewer='worker', nominee='worker' WHERE id=?",
                (iid,))
    con.commit()
    stats = migrate_assignees(con, apply=True)
    assert stats["reviewer_normalized"] == 1 and stats["nominee_normalized"] == 1
    row = con.execute("SELECT reviewer, nominee FROM issues WHERE id=?",
                      (iid,)).fetchone()
    assert row["reviewer"] == "worker@t" and row["nominee"] == "worker@t"
