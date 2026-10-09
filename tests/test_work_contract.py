import json

import pytest
from fastapi.testclient import TestClient

from app import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("TT_CONTRACT_VERSION", "2.1")
    return TestClient(create_app(str(tmp_path / "contract.db")))


def start(client, title="change behavior"):
    issue = client.post("/issues", json={"acceptance": "완료 기준: 테스트 통과", "title": title}).json()
    response = client.post(f"/issues/{issue['id']}/claim", json={"agent": "worker"})
    assert response.status_code == 200
    return response.json()


def test_claim_and_pull_deliver_the_published_contract(client):
    response = client.get("/work-contract")
    assert response.status_code == 200
    contract = response.json()
    assert contract["version"] and "TDD" in contract["instructions"]
    assert "fail" in contract["instructions"] and "verification" in contract["instructions"]
    assert contract["instructions"] in client.get("/api.md").text
    claimed = start(client)
    assert claimed["work_contract"] == contract
    assert claimed["execution_attempt"] == 1
    other = client.post("/issues", json={"acceptance": "완료 기준: 테스트 통과", "title": "pull task"}).json()
    pulled = client.post("/pull", json={"agent": "puller"}).json()
    assert pulled["id"] == other["id"] and pulled["work_contract"] == contract
    assert pulled["execution_attempt"] == 1


def test_dispatch_delivers_contract_without_changing_original_message(client, monkeypatch):
    import app as appmod

    payloads = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, *args):
            return b"{}"

    def receive(request, **kwargs):
        payloads.append(json.loads(request.data))
        return Response()

    import urllib.request as _urlreq
    monkeypatch.setattr(_urlreq, "urlopen", receive)  # M14N: 로직이 service.py로 이동 — 전역 모듈 싱글턴 직접 패치
    issue = start(client)
    client.post("/agents", json={"name": "runner", "base_url": "http://receiver/hook"})
    message = '#opts {"model":"chosen"}\nFix the retry bug'
    response = client.post(f"/issues/{issue['id']}/dispatch", json={"agent": "runner", "message": message})
    assert response.status_code == 201 and response.json()["status"] == "ok"
    assert payloads[0]["message"] == message
    assert payloads[0]["work_contract"] == client.get("/work-contract").json()
    assert payloads[0]["execution_attempt"] == 1


@pytest.mark.parametrize("body", [
    "pytest FAILED: 12 errors", "pytest 1 passed, 2 failed", "증거:",
    "참고 자료: https://example.com/requirements", "아직 pytest 실행하지 않음",
    "commit deadbeef", "pytest green",
])
def test_non_results_do_not_become_verified(client, body):
    issue = start(client)
    client.post(f"/issues/{issue['id']}/comments", json={"author": "worker", "body": body})
    response = client.patch(f"/issues/{issue['id']}", json={"state": "done"})
    assert response.json()["state"] == "review"
    assert response.json()["verified"] == 0


def test_verify_rejects_arbitrary_text(client):
    issue = start(client)
    client.patch(f"/issues/{issue['id']}", json={"state": "done"})
    response = client.post(f"/issues/{issue['id']}/verify", json={"verifier": "worker", "evidence": "x"})
    assert response.status_code == 422
    assert client.get(f"/issues/{issue['id']}").json()["state"] == "review"


@pytest.mark.parametrize("state", ["done", "review", "in_progress"])
def test_creation_cannot_skip_the_workflow(client, state):
    assert client.post("/issues", json={"acceptance": "완료 기준: 테스트 통과", "title": "skip", "state": state}).status_code == 422


def test_reopening_invalidates_evidence_and_starts_a_new_attempt(client):
    issue = start(client)
    path = f"/issues/{issue['id']}"
    client.post(path + "/comments", json={"author": "worker", "body": "pytest 5 passed"})
    assert client.patch(path, json={"state": "done"}).json()["state"] == "done"
    reopened = client.patch(path, json={"state": "todo"}).json()
    assert reopened["verified"] == 0 and reopened["verified_at"] is None
    assert reopened["completed_at"] is None and reopened["verified_evidence"] == ""
    claimed = client.post(path + "/claim", json={"agent": "worker"}).json()
    assert claimed["execution_attempt"] > issue["execution_attempt"]
    assert client.patch(path, json={"state": "done"}).json()["state"] == "review"


def test_scope_change_cannot_reuse_an_old_comment_even_in_same_patch(client):
    issue = start(client)
    path = f"/issues/{issue['id']}"
    client.post(path + "/comments", json={"author": "worker", "body": "pytest 5 passed"})
    response = client.patch(path, json={"body": "different acceptance criteria", "state": "done"})
    assert response.json()["state"] == "review"
    assert response.json()["verified"] == 0


def report(issue, **fields):
    ver = issue["work_contract"]["version"]
    base = {"contract_version": ver, "attempt": issue["execution_attempt"]}
    if ver.startswith("tt-tdd-v1"):
        base.update({"method": "tdd", "red_command": "pytest tests/test_retry.py",
                     "red_evidence": "1 failed: expected one notification, observed two",
                     "command": "pytest tests/test_retry.py", "result": "passed",
                     "evidence": "1 passed; duplicate notification no longer reproduced"})
        base.update(fields)
        return base
    if ver.startswith("tt-tdd-v2.1"):
        ev = [{"command": "pytest tests/test_retry.py", "exit_code": 0,
               "output_snippet": "1 passed; duplicate notification no longer reproduced"}]
        verif = {"commands": "pytest tests/test_retry.py", "evidence": ev}
        if "verification" in base and isinstance(base["verification"], dict) and isinstance(
                base["verification"].get("evidence"), str):
            base["verification"]["evidence"] = base["verification"]["evidence"]
            return {**{"method": "tdd",
                       "design": {"criteria": "중복 알림 제거", "verification": "pytest 실행",
                                  "evidence": "수정 전: 1 failed — 알림 2회 관측"},
                       "implementation": {"summary": "알림 중복 제거", "commands": "edit"},
                       "verification": {"commands": "pytest tests/test_retry.py",
                                        "evidence": "1 passed"}, "result": "passed"}, **base}
        base.update({"method": "tdd",
                     "design": {"criteria": "중복 알림 제거", "verification": "pytest 실행",
                                "evidence": "수정 전: 1 failed — 알림 2회 관측"},
                     "implementation": {"summary": "알림 중복 제거", "commands": "edit"},
                     "verification": verif, "result": "passed"})
        base.update(fields)
        return base
    base.update({"method": "tdd",
                 "design": {"criteria": "중복 알림 제거", "verification": "pytest 실행",
                            "evidence": "수정 전: 1 failed — 알림 2회 관측"},
                 "implementation": {"summary": "알림 중복 제거", "commands": "edit"},
                 "verification": {"commands": "pytest tests/test_retry.py",
                                  "evidence": "1 passed; duplicate notification no longer reproduced"},
                 "result": "passed"})
    base.update(fields)
    return base


def test_tdd_report_is_recorded_as_reported_evidence(client):
    issue = start(client)
    proof = report(issue)
    response = client.patch(f"/issues/{issue['id']}", json={"state": "done", "completion_report": proof})
    assert response.status_code == 200
    data = response.json()
    assert data["state"] == "review" and data["verification_status"] == "reported"
    if data["completion_report"]["contract_version"].startswith("tt-tdd-v1"):
        assert data["completion_report"]["red_evidence"] == proof["red_evidence"]
        assert data["completion_report"]["evidence"] == proof["evidence"]
    else:
        assert data["completion_report"]["design"]["evidence"]
        assert data["completion_report"]["verification"]["evidence"]


@pytest.mark.parametrize("overrides", [
    {"red_command": ""}, {"red_evidence": ""}, {"command": " "},
    {"method": "alternative", "reason": ""},
])
def test_incomplete_report_is_rejected(tmp_path, monkeypatch, overrides):
    monkeypatch.setenv("TT_CONTRACT_VERSION", "1")
    client = TestClient(create_app(str(tmp_path / "contract-v1.db")))
    issue = start(client)
    response = client.patch(f"/issues/{issue['id']}", json={"state": "done", "completion_report": report(issue, **overrides)})
    assert response.status_code == 422


@pytest.mark.parametrize("result", ["failed", "inconclusive"])
def test_unsuccessful_report_cannot_close_issue(client, result):
    issue = start(client)
    path = f"/issues/{issue['id']}"
    proof = report(issue, result=result, limitations="test environment unavailable")
    data = client.patch(path, json={"state": "done", "completion_report": proof}).json()
    assert data["state"] == "review" and data["verified"] == 0
    assert data["completion_report"]["result"] == result
    assert client.post(path + "/verify", json={"verifier": "worker", "completion_report": proof}).status_code == 422


def test_stale_report_is_rejected_after_a_new_claim(client):
    issue = start(client)
    proof = report(issue)
    path = f"/issues/{issue['id']}"
    client.patch(path, json={"state": "todo"})
    client.post(path + "/claim", json={"agent": "worker"})
    assert client.patch(path, json={"state": "done", "completion_report": proof}).status_code == 409


def test_approval_exception_is_distinct_from_test_report(client):
    issue = start(client)
    data = client.patch(f"/issues/{issue['id']}", json={"state": "done", "force_done": True}).json()
    assert data["state"] == "done" and data["verification_status"] == "approved"


def test_latest_failed_result_does_not_fall_back_to_old_pass(client):
    issue = start(client)
    path = f"/issues/{issue['id']}"
    for body in ["pytest 5 passed", "pytest FAILED: regression found"]:
        client.post(path + "/comments", json={"author": "worker", "body": body})
    assert client.patch(path, json={"state": "done"}).json()["state"] == "review"


def test_scope_edit_invalidates_a_completed_card(client):
    issue = start(client)
    path = f"/issues/{issue['id']}"
    client.patch(path, json={"state": "done", "completion_report": report(issue)})
    changed = client.patch(path, json={"body": "new scope"}).json()
    assert changed["state"] == "review" and changed["verified"] == 0
    assert changed["completion_report"] is None and changed["completed_at"] is None


def test_alternative_report_requires_reason_but_not_red(tmp_path, monkeypatch):
    monkeypatch.setenv("TT_CONTRACT_VERSION", "1")
    client = TestClient(create_app(str(tmp_path / "contract-v1-alt.db")))
    issue = start(client)
    proof = report(issue, method="alternative", reason="documentation-only", red_command="", red_evidence="")
    response = client.patch(f"/issues/{issue['id']}", json={"state": "done", "completion_report": proof})
    # M3R7M0ZR-YF99: 유효 보고(v1 alternative 포함)도 review에 정지 — done은 확인 경로만
    assert response.status_code == 200 and response.json()["state"] == "review"


def test_contract_mismatch_and_scope_change_reject_stale_report(client):
    issue = start(client)
    path = f"/issues/{issue['id']}"
    stale_v2 = report(issue)
    stale_v2["contract_version"] = "tt-tdd-v2:deadbeef"
    stale_v2["verification"]["evidence"] = "1 passed"
    assert client.patch(path, json={"state": "done", "completion_report": stale_v2}).status_code == 409
    assert client.patch(path, json={"state": "done", "body": "new scope", "completion_report": report(issue)}).status_code == 409
    assert client.get(path).json()["body"] == ""  # rejected mutation is atomic


def test_expired_pull_resets_evidence(client):
    import db
    issue = start(client)
    path = f"/issues/{issue['id']}"
    client.post(path + "/comments", json={"author": "worker", "body": "pytest 5 passed"})
    with db.connect(client.app.state.db_path) as con:
        con.execute("UPDATE issues SET lease_expires='2000-01-01' WHERE id=?", (issue["id"],))
    pulled = client.post("/pull", json={"agent": "new-worker"}).json()
    assert pulled["execution_attempt"] == issue["execution_attempt"] + 1
    assert client.patch(path, json={"state": "done"}).json()["state"] == "review"


def test_failed_report_cannot_reuse_old_pass_comment(client):
    issue = start(client)
    path = f"/issues/{issue['id']}"
    client.post(path + "/comments", json={"author": "worker", "body": "pytest 5 passed"})
    client.patch(path, json={"state": "done", "completion_report": report(issue, result="failed")})
    assert client.patch(path, json={"state": "done"}).json()["state"] == "review"
    assert client.post(path + "/verify", json={"verifier": "worker"}).status_code == 422


def test_zero_failures_does_not_invalidate_pass(client):
    issue = start(client)
    path = f"/issues/{issue['id']}"
    client.post(path + "/comments", json={"author": "worker", "body": "pytest 5 passed, 0 failed, 0 errors"})
    assert client.patch(path, json={"state": "done"}).json()["state"] == "done"


def test_required_report_policy_closes_legacy_completion_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("TT_REQUIRE_REPORT", "1")
    strict = TestClient(create_app(str(tmp_path / "strict.db")))
    issue = start(strict)
    path = f"/issues/{issue['id']}"
    assert issue["work_contract"]["report_required"] is True
    strict.post(path + "/comments", json={"author": "worker", "body": "pytest 5 passed"})
    assert strict.patch(path, json={"state": "done"}).json()["state"] == "review"
    assert strict.post(path + "/verify", json={"verifier": "worker", "evidence": "pytest 5 passed"}).status_code == 422
    result = strict.post(path + "/verify", json={"verifier": "worker", "completion_report": report(issue)})
    assert result.status_code == 200 and result.json()["verification_status"] == "reported"


def test_required_report_policy_has_explicit_approval_exception(tmp_path, monkeypatch):
    monkeypatch.setenv("TT_REQUIRE_REPORT", "1")
    strict = TestClient(create_app(str(tmp_path / "strict.db")))
    issue = start(strict)
    data = strict.patch(f"/issues/{issue['id']}", json={"state": "done", "force_done": True}).json()
    assert data["state"] == "done" and data["verification_status"] == "approved"


def test_migration_keeps_historical_data_and_marks_legacy(tmp_path):
    import sqlite3
    import db
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as con:
        con.execute("""CREATE TABLE issues (
            id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT NOT NULL DEFAULT '',
            state TEXT NOT NULL DEFAULT 'todo', priority INTEGER, labels TEXT NOT NULL DEFAULT '',
            assignee TEXT NOT NULL DEFAULT '', parent_id TEXT, version INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, started_at TEXT, completed_at TEXT,
            verified INTEGER NOT NULL DEFAULT 0, verified_evidence TEXT NOT NULL DEFAULT '')""")
        con.execute("INSERT INTO issues (id,title,state,created_at,updated_at,verified,verified_evidence) VALUES ('old','keep','done','before','before',1,'original evidence')")
    for _ in range(2):  # migration is idempotent
        with db.connect(str(path)) as con:
            row = db.to_dict(con.execute("SELECT * FROM issues WHERE id='old'").fetchone())
            assert row["title"] == "keep" and row["version"] == 1
            assert row["verified_evidence"] == "original evidence" and row["verified"] == 1
            assert row["verification_status"] == "legacy" and row["completion_report"] is None
            assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


@pytest.mark.parametrize("mode", ["warn", "off"])
def test_required_report_overrides_permissive_done_gate(tmp_path, monkeypatch, mode):
    monkeypatch.setenv("TT_REQUIRE_REPORT", "1")
    monkeypatch.setenv("TT_DONE_GATE", mode)  # M14N: DONE_GATE lazy read(config.done_gate) — env 주입으로 동일 의미
    strict = TestClient(create_app(str(tmp_path / "required.db")))
    issue = start(strict)
    assert strict.patch(f"/issues/{issue['id']}", json={"state": "done"}).json()["state"] == "review"


def test_policy_is_pinned_until_next_claim(tmp_path, monkeypatch):
    path = str(tmp_path / "pinned.db")
    monkeypatch.setenv("TT_REQUIRE_REPORT", "1")
    first = TestClient(create_app(path))
    issue = start(first)
    monkeypatch.setenv("TT_REQUIRE_REPORT", "0")
    restarted = TestClient(create_app(path))
    assert restarted.get("/work-contract").json()["version"] != issue["work_contract"]["version"]
    current = restarted.get(f"/issues/{issue['id']}").json()
    assert current["work_contract"] == issue["work_contract"]
    assert restarted.patch(f"/issues/{issue['id']}", json={"state": "done"}).json()["state"] == "review"
    result = restarted.post(f"/issues/{issue['id']}/verify", json={"verifier": "worker", "completion_report": report(issue)})
    assert result.status_code == 200
    restarted.patch(f"/issues/{issue['id']}", json={"state": "todo"})
    new = restarted.post(f"/issues/{issue['id']}/claim", json={"agent": "worker"}).json()
    assert new["work_contract"]["report_required"] is False
    assert new["execution_attempt"] > issue["execution_attempt"]


@pytest.mark.parametrize("existing_state", ["in_progress", "review"])
def test_upgrade_keeps_unpinned_work_compatible_until_next_claim(tmp_path, monkeypatch, existing_state):
    """Migrated live work has no contract; a server flag must not strand it."""
    import db
    monkeypatch.setenv("TT_REQUIRE_REPORT", "1")
    path = str(tmp_path / "upgrade.db")
    strict = TestClient(create_app(path))
    issue = strict.post("/issues", json={"acceptance": "완료 기준: 테스트 통과", "title": "existing production work"}).json()
    iid = issue["id"]
    # Same persisted fields as a pre-contract server's in-flight row.
    with db.connect(path) as con:
        con.execute("UPDATE issues SET state=?, assignee='legacy-worker' WHERE id=?", (existing_state, iid))
    old = strict.get(f"/issues/{iid}").json()
    assert old["work_contract"] is None and old["execution_attempt"] == 0
    strict.post(f"/issues/{iid}/comments", json={"author": "legacy-worker", "body": "pytest 5 passed"})
    if existing_state == "review":
        done = strict.post(f"/issues/{iid}/verify", json={"verifier": "legacy-worker"})
    else:
        done = strict.patch(f"/issues/{iid}", json={"state": "done"})
    assert done.status_code == 200 and done.json()["state"] == "done", done.text
    assert done.json()["verification_status"] == "reported"

    strict.patch(f"/issues/{iid}", json={"state": "todo"})
    claimed = strict.post(f"/issues/{iid}/claim", json={"agent": "worker"}).json()
    assert claimed["work_contract"]["report_required"] is True
    strict.post(f"/issues/{iid}/comments", json={"author": "worker", "body": "pytest 5 passed"})
    assert strict.patch(f"/issues/{iid}", json={"state": "done"}).json()["state"] == "review"
    result = strict.post(f"/issues/{iid}/verify", json={"verifier": "worker", "completion_report": report(claimed)})
    assert result.status_code == 200 and result.json()["state"] == "done"


def v2_report(attempt):
    return {
        "contract_version": "IGNORED",
        "attempt": attempt,
        "method": "planned",
        "design": {"criteria": "3단계 보고서가 검증된다", "verification": "test_work_contract의 이 테스트(수정 전 실패)"},
        "implementation": {"summary": "v2 계약 스위치", "commands": "edit work_contract.py + api.md"},
        "verification": {"commands": "pytest tests/test_work_contract.py -q", "evidence": "passed"},
        "result": "passed",
        "limitations": "",
    }


def test_env_switch_serves_v2_contract(tmp_path, monkeypatch):
    monkeypatch.setenv("TT_CONTRACT_VERSION", "2")
    c = TestClient(create_app(str(tmp_path / "v2.db")))
    contract = c.get("/work-contract").json()
    assert contract["version"].startswith("tt-tdd-v2:")
    assert "three stages" in contract["instructions"]
    issue = c.post("/issues", json={"acceptance": "완료 기준: 테스트 통과", "title": "v2 flow"}).json()
    claimed = c.post(f"/issues/{issue['id']}/claim", json={"agent": "worker"}).json()
    assert claimed["work_contract"]["version"].startswith("tt-tdd-v2:")
    report = v2_report(claimed["execution_attempt"])
    report["contract_version"] = claimed["work_contract"]["version"]
    done = c.patch(f"/issues/{issue['id']}", json={
        "state": "done", "version": issue["version"] + 1, "completion_report": report})
    assert done.status_code == 200, done.text
    # M3R7M0ZR-YF99: v2 제출의 종착지는 review(done 아님) — done은 merge 확인/probe verify
    assert done.json()["state"] == "review" and done.json()["verification_status"] == "reported"
    # 보고 없이 done 시도(재개 후)는 review 강등 — 3단계 없이는 자기완결 인정 없음
    c.patch(f"/issues/{issue['id']}", json={"state": "todo", "version": done.json()["version"]})
    again = c.post(f"/issues/{issue['id']}/claim", json={"agent": "worker"}).json()
    reopened = c.patch(f"/issues/{issue['id']}", json={"state": "in_progress", "version": again["version"]})
    demoted = c.patch(f"/issues/{issue['id']}", json={"state": "done", "version": reopened.json()["version"]})
    assert demoted.status_code == 200 and demoted.json()["state"] == "review", demoted.text


def test_v1_report_rejected_on_v2_pinned_card(tmp_path, monkeypatch):
    monkeypatch.setenv("TT_CONTRACT_VERSION", "2")
    c = TestClient(create_app(str(tmp_path / "v2b.db")))
    issue = c.post("/issues", json={"acceptance": "완료 기준: 테스트 통과", "title": "mixed"}).json()
    claimed = c.post(f"/issues/{issue['id']}/claim", json={"agent": "worker"}).json()
    legacy = {"contract_version": claimed["work_contract"]["version"], "attempt": claimed["execution_attempt"],
              "method": "tdd", "red_command": "pytest", "red_evidence": "1 failed",
              "command": "pytest", "result": "passed", "evidence": "1 passed"}
    response = c.patch(f"/issues/{issue['id']}", json={
        "state": "done", "version": issue["version"] + 1, "completion_report": legacy})
    assert response.status_code == 422


def test_mixed_v1_and_v2_claims_one_db(tmp_path, monkeypatch):
    db = str(tmp_path / "mixed.db")
    monkeypatch.setenv("TT_CONTRACT_VERSION", "1")
    c = TestClient(create_app(db))
    old_issue = c.post("/issues", json={"acceptance": "완료 기준: 테스트 통과", "title": "pinned v1"}).json()
    old_claim = c.post(f"/issues/{old_issue['id']}/claim", json={"agent": "w1"}).json()
    assert old_claim["work_contract"]["version"].startswith("tt-tdd-v1:")
    monkeypatch.setenv("TT_CONTRACT_VERSION", "2")
    c2 = TestClient(create_app(db))
    new_issue = c2.post("/issues", json={"acceptance": "완료 기준: 테스트 통과", "title": "pinned v2"}).json()
    new_claim = c2.post(f"/issues/{new_issue['id']}/claim", json={"agent": "w2"}).json()
    assert new_claim["work_contract"]["version"].startswith("tt-tdd-v2:")
    legacy = {"contract_version": old_claim["work_contract"]["version"], "attempt": old_claim["execution_attempt"],
              "method": "tdd", "red_command": "pytest", "red_evidence": "1 failed",
              "command": "pytest", "result": "passed", "evidence": "1 passed"}
    r1 = c.patch(f"/issues/{old_issue['id']}", json={
        "state": "done", "version": old_issue["version"] + 1, "completion_report": legacy})
    assert r1.status_code == 200, r1.text
    report = v2_report(new_claim["execution_attempt"])
    report["contract_version"] = new_claim["work_contract"]["version"]
    r2 = c2.patch(f"/issues/{new_issue['id']}", json={
        "state": "done", "version": new_issue["version"] + 1, "completion_report": report})
    assert r2.status_code == 200, r2.text


# --- THNJ: 약한 합의 — 사람 자기선언 verify (human=true) ---

def test_human_verify_자기선언_승인(client):
    issue = start(client)
    r = client.post(f"/issues/{issue['id']}/verify",
                    json={"verifier": "skshim", "evidence": "브라우저에서 자료실 확인 — 빈 결과 없음",
                          "human": True})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["state"] == "done" and data["verification_status"] == "approved"
    comments = client.get(f"/issues/{issue['id']}").json()["comments"]
    assert any("사람 승인" in c["body"] and "skshim" in c["body"] for c in comments)


def test_human_verify는_보고요구와_정규식_게이트_무시(client, monkeypatch):
    monkeypatch.setenv("TT_REQUIRE_REPORT", "1")
    issue = start(client)
    # human=false면 두 게이트 중 하나는 반드시 걸림(정규식 불일치) — 회귀 확인
    r_agent = client.post(f"/issues/{issue['id']}/verify",
                          json={"verifier": "some-agent", "evidence": "화면에서 확인했음"})
    assert r_agent.status_code in (409, 422)  # 게이트 순서 무관 — agent 경로는 반드시 거부
    # human=true면 한 줄 노트로 통과
    r_human = client.post(f"/issues/{issue['id']}/verify",
                          json={"verifier": "skshim", "evidence": "화면에서 확인했음", "human": True})
    assert r_human.status_code == 200
    assert r_human.json()["verification_status"] == "approved"


def test_human_verify_노트_없으면_거부(client):
    issue = start(client)
    r = client.post(f"/issues/{issue['id']}/verify",
                    json={"verifier": "skshim", "evidence": "  ", "human": True})
    assert r.status_code == 422


def test_human_자가승인은_허용하되_이름_나란히_기록(client):
    issue = start(client)  # assignee=worker
    r = client.post(f"/issues/{issue['id']}/verify",
                    json={"verifier": "worker", "evidence": "직접 확인", "human": True})
    assert r.status_code == 200
    comments = client.get(f"/issues/{issue['id']}").json()["comments"]
    body = next(c["body"] for c in comments if "사람 승인" in c["body"])
    assert "worker" in body and "assignee" in body.lower() or "작업" in body


def test_claim_review는_review상태에서만_가능하고_상태를_바꾸지_않는다(client):
    issue = client.post("/issues", json={"acceptance": "완료 기준: 테스트 통과", "title": "리뷰 대상"}).json()
    r = client.post(f"/issues/{issue['id']}/claim-review", json={"agent": "codex"})
    assert r.status_code == 409  # todo 상태에는 리뷰어 claim 불가

    claimed = start(client)
    r = client.post(f"/issues/{claimed['id']}/claim-review", json={"agent": "codex"})
    assert r.status_code == 409  # in_progress도 불가

    client.patch(f"/issues/{claimed['id']}", json={"state": "done", "completion_report": {
        "contract_version": claimed["work_contract"]["version"], "attempt": 1, "method": "planned",
        "design": {"criteria": "c", "verification": "v"},
        "implementation": {"summary": "s", "commands": "cc"},
        "verification": {"commands": "ccc", "evidence": "e"},
        "result": "passed", "limitations": ""}})
    r = client.post(f"/issues/{claimed['id']}/claim-review", json={"agent": "codex"})
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "review"          # 상태 불변
    assert body["assignee"] == "worker"       # 작업자 assignee 보존(review-fix 대상)
    assert body["reviewer"] == "codex"        # 리뷰어 점유 표기
    assert body["execution_attempt"] == 1     # attempt 불변
    assert body["lease_by"] == "codex"

    r = client.post(f"/issues/{claimed['id']}/claim-review", json={"agent": "claude"})
    assert r.status_code == 409  # 이중 점유 불가

    # 판정 후 리뷰어 반납 — probe release-reviewer 경로
    r = client.patch(f"/issues/{claimed['id']}", json={"version": body["version"], "reviewer": ""})
    assert r.status_code == 200
    assert r.json()["reviewer"] is None


def test_probe_반납은_실제_핸들러에서_reviewer와_lease를_지우고_assignee를_보존한다(client, monkeypatch):
    """R1 통합 회귀 — probe execute → 실제 PATCH → reviewer/lease 해제, 작업자 assignee 유지."""
    import probe.core

    issue = client.post("/issues", json={"acceptance": "완료 기준: 테스트 통과", "title": "반납 통합"}).json()
    claimed = client.post(f"/issues/{issue['id']}/claim", json={"agent": "worker"}).json()
    client.patch(f"/issues/{issue['id']}", json={"state": "done", "completion_report": {
        "contract_version": claimed["work_contract"]["version"], "attempt": 1, "method": "planned",
        "design": {"criteria": "c", "verification": "v"},
        "implementation": {"summary": "s", "commands": "cc"},
        "verification": {"commands": "ccc", "evidence": "e"},
        "result": "passed", "limitations": ""}})
    body = client.post(f"/issues/{issue['id']}/claim-review", json={"agent": "codex"}).json()
    assert body["reviewer"] == "codex" and body["lease_by"] == "codex"
    # 유효 판정 기록(R7 필터 통과용) — 코멘트가 버전을 올리므로 반납 액션은 최신 버전 기준
    client.post(f"/issues/{issue['id']}/comments", json={
        "author": "codex", "body": "review: approve\nPR#1@aaaaaaaa"})
    cur_version = client.get(f"/issues/{issue['id']}").json()["version"]

    def fake_api(url, path, method="GET", payload=None):
        if method == "GET":
            return client.get(path).json()
        return client.request(method, path, json=payload).json()

    monkeypatch.setattr(probe.core, "api", fake_api)
    probe.core.execute("u", {"issue": issue["id"], "action": "release-reviewer",
                             "reviewer": "codex", "pr": 1, "head_sha": "a" * 8,
                             "expected_version": cur_version})
    d = client.get(f"/issues/{issue['id']}").json()
    assert d["reviewer"] is None
    assert d["lease_by"] == "" and d["lease_expires"] is None
    assert d["assignee"] == "worker"  # review-fix 대상 보존
    assert d["state"] == "review" and d["execution_attempt"] == 1


def test_리뷰어_lease만료_후_타_리뷰어_인계_가능(client):
    import sqlite3

    issue = client.post("/issues", json={"acceptance": "완료 기준: 테스트 통과", "title": "인계"}).json()
    claimed = client.post(f"/issues/{issue['id']}/claim", json={"agent": "worker"}).json()
    client.patch(f"/issues/{issue['id']}", json={"state": "done", "completion_report": {
        "contract_version": claimed["work_contract"]["version"], "attempt": 1, "method": "planned",
        "design": {"criteria": "c", "verification": "v"},
        "implementation": {"summary": "s", "commands": "cc"},
        "verification": {"commands": "ccc", "evidence": "e"},
        "result": "passed", "limitations": ""}})
    assert client.post(f"/issues/{issue['id']}/claim-review", json={"agent": "codex"}).status_code == 200
    con = sqlite3.connect(client.app.state.db_path)
    con.execute("UPDATE issues SET lease_expires='2000-01-01T00:00:00+00:00' WHERE id=?", (issue["id"],))
    con.commit(); con.close()
    r = client.post(f"/issues/{issue['id']}/claim-review", json={"agent": "claude"})
    assert r.status_code == 200
    assert r.json()["reviewer"] == "claude"


def test_리뷰_dispatch는_리뷰_계약을_전달한다(client, monkeypatch):
    """R2 — 리뷰 dispatch payload의 work_contract가 리뷰 계약이고, claim-review 응답도 리뷰 계약."""
    import probe.core
    import service

    monkeypatch.setenv("TT_REVIEW_AGENT", "codex")
    payloads = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self):
            return b"{}"

    def fake_urlopen(req, timeout=10):
        payloads.append(json.loads(req.data))
        return Response()

    monkeypatch_dummy = None
    issue = client.post("/issues", json={"acceptance": "완료 기준: 테스트 통과", "title": "리뷰 계약"}).json()
    # dispatch는 등록된 webhook agent 필요 — codex 등록 후 호출
    client.post("/agents", json={"name": "codex", "base_url": "http://runner.local"})
    probe_api = probe.core.api

    monkeypatch_urlopen = None
    # probe execute의 api를 TestClient로 대체해 실제 dispatch 엔드포인트를 태운다
    def fake_api(url, path, method="GET", payload=None):
        if method == "GET":
            return client.get(path).json()
        return client.request(method, path, json=payload).json()

    import probe.core as pc
    pc.api = fake_api
    try:
        import urllib.request as _ur
        orig = _ur.urlopen
        _ur.urlopen = fake_urlopen  # service.deliver가 보내는 실제 webhook 페이로드 관찰
        pc.execute("u", {"issue": issue["id"], "action": "review-request", "pr": 5,
                         "repo": "plainOldCode/think-tank", "branch": "tt/x",
                         "head_sha": "b" * 40, "marker": "[review-req #5/bbbbbbbb]"})
    finally:
        _ur.urlopen = orig
        pc.api = probe_api
    review_payloads = [p for p in payloads if "[auto review]" in (p.get("message") or "")]
    assert review_payloads, "리뷰 dispatch가 전송되지 않음"
    assert review_payloads[0]["work_contract"] == service.REVIEW_CONTRACT
    assert review_payloads[0]["work_contract"]["role"] == "reviewer"

    # claim-review 응답도 리뷰 계약을 반환 — 작업자 구현 계약과 혼선 방지
    claimed = client.post(f"/issues/{issue['id']}/claim", json={"agent": "worker"}).json()
    client.patch(f"/issues/{issue['id']}", json={"state": "done", "completion_report": {
        "contract_version": claimed["work_contract"]["version"], "attempt": 1, "method": "planned",
        "design": {"criteria": "c", "verification": "v"},
        "implementation": {"summary": "s", "commands": "cc"},
        "verification": {"commands": "ccc", "evidence": "e"},
        "result": "passed", "limitations": ""}})
    r = client.post(f"/issues/{issue['id']}/claim-review", json={"agent": "codex"}).json()
    assert r["work_contract"] == service.REVIEW_CONTRACT


def test_리뷰어_재claim은_한도_제외되고_타인은_한도_적용(client, monkeypatch):
    """R4 — 같은 카드 재claim(refresh)은 현재 카드 lease를 한도에서 제외."""
    monkeypatch.setenv("TT_MAX_LEASES", "1")
    issue = client.post("/issues", json={"acceptance": "완료 기준: 테스트 통과", "title": "한도"}).json()
    claimed = client.post(f"/issues/{issue['id']}/claim", json={"agent": "worker"}).json()
    client.patch(f"/issues/{issue['id']}", json={"state": "done", "completion_report": {
        "contract_version": claimed["work_contract"]["version"], "attempt": 1, "method": "planned",
        "design": {"criteria": "c", "verification": "v"},
        "implementation": {"summary": "s", "commands": "cc"},
        "verification": {"commands": "ccc", "evidence": "e"},
        "result": "passed", "limitations": ""}})
    assert client.post(f"/issues/{issue['id']}/claim-review", json={"agent": "codex"}).status_code == 200
    # 재claim(갱신) — 자기 lease가 한도 1에 걸려도 본인 카드라 허용
    assert client.post(f"/issues/{issue['id']}/claim-review", json={"agent": "codex"}).status_code == 200
    # 타 에이전트가 별도 카드를 이미 점유 중이면 다른 리뷰 카드 선점은 한도로 거부
    other = client.post("/issues", json={"acceptance": "완료 기준: 테스트 통과", "title": "다른 카드"}).json()
    client.post(f"/issues/{other['id']}/claim", json={"agent": "claude"})
    other2 = client.post("/issues", json={"acceptance": "완료 기준: 테스트 통과", "title": "다른 리뷰"}).json()
    o2 = client.post(f"/issues/{other2['id']}/claim", json={"agent": "worker"}).json()
    client.patch(f"/issues/{other2['id']}", json={"state": "done", "completion_report": {
        "contract_version": o2["work_contract"]["version"], "attempt": 1, "method": "planned",
        "design": {"criteria": "c", "verification": "v"},
        "implementation": {"summary": "s", "commands": "cc"},
        "verification": {"commands": "ccc", "evidence": "e"},
        "result": "passed", "limitations": ""}})
    r = client.post(f"/issues/{other2['id']}/claim-review", json={"agent": "claude"})
    assert r.status_code == 409 and "lease limit" in r.json()["detail"]


def test_stale_반납은_재작업자_lease를_보존한다(client, monkeypatch):
    """R5 — release-reviewer 실행 시 lease_by가 다른 에이전트(재작업자)면 lease를 건드리지 않는다."""
    import probe.core

    issue = client.post("/issues", json={"acceptance": "완료 기준: 테스트 통과", "title": "stale 반납"}).json()
    claimed = client.post(f"/issues/{issue['id']}/claim", json={"agent": "worker"}).json()
    client.patch(f"/issues/{issue['id']}", json={"state": "done", "completion_report": {
        "contract_version": claimed["work_contract"]["version"], "attempt": 1, "method": "planned",
        "design": {"criteria": "c", "verification": "v"},
        "implementation": {"summary": "s", "commands": "cc"},
        "verification": {"commands": "ccc", "evidence": "e"},
        "result": "passed", "limitations": ""}})
    body = client.post(f"/issues/{issue['id']}/claim-review", json={"agent": "codex"}).json()
    # 유효 판정 기록(R7 필터 통과용) — codex 판정 PR#1@aaaaaaaa
    client.post(f"/issues/{issue['id']}/comments", json={
        "author": "codex", "body": "review: approve\nPR#1@aaaaaaaa"})
    cur_version = client.get(f"/issues/{issue['id']}").json()["version"]
    # 재작업자가 lease를 넘겨받은 상태(리뷰어 claim은 아직 미반납 — stale 액션 도착 시나리오)
    import sqlite3
    con = sqlite3.connect(client.app.state.db_path)
    con.execute("UPDATE issues SET lease_by='worker', lease_expires='2099-01-01T00:00:00+00:00' WHERE id=?", (issue["id"],))
    con.commit(); con.close()

    def fake_api(url, path, method="GET", payload=None):
        if method == "GET":
            return client.get(path).json()
        return client.request(method, path, json=payload).json()

    monkeypatch.setattr(probe.core, "api", fake_api)
    probe.core.execute("u", {"issue": issue["id"], "action": "release-reviewer", "reviewer": "codex",
                             "pr": 1, "head_sha": "a" * 8,
                             "expected_version": cur_version})
    d = client.get(f"/issues/{issue['id']}").json()
    assert d["reviewer"] is None          # 리뷰어 점유는 해제
    assert d["lease_by"] == "worker"      # 재작업자 lease 보존
    assert d["assignee"] == "worker"


def test_작업자_재claim은_이전_리뷰어_표기를_제거한다(client):
    """R6 — review→todo→claim 후에도 reviewer가 남는 것 방지."""
    issue = client.post("/issues", json={"acceptance": "완료 기준: 테스트 통과", "title": "R6"}).json()
    claimed = client.post(f"/issues/{issue['id']}/claim", json={"agent": "worker"}).json()
    client.patch(f"/issues/{issue['id']}", json={"state": "done", "completion_report": {
        "contract_version": claimed["work_contract"]["version"], "attempt": 1, "method": "planned",
        "design": {"criteria": "c", "verification": "v"},
        "implementation": {"summary": "s", "commands": "cc"},
        "verification": {"commands": "ccc", "evidence": "e"},
        "result": "passed", "limitations": ""}})
    assert client.post(f"/issues/{issue['id']}/claim-review", json={"agent": "codex"}).status_code == 200
    # 재작업: review → todo → claim
    client.patch(f"/issues/{issue['id']}", json={"state": "todo"})
    c2 = client.post(f"/issues/{issue['id']}/claim", json={"agent": "worker2"}).json()
    assert c2["reviewer"] is None
    assert c2["state"] == "in_progress"
