import json

import pytest
from fastapi.testclient import TestClient

from app import create_app


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(str(tmp_path / "contract.db")))


def start(client, title="change behavior"):
    issue = client.post("/issues", json={"title": title}).json()
    response = client.post(f"/issues/{issue['id']}/claim", json={"agent": "worker"})
    assert response.status_code == 200
    return response.json()


def test_claim_and_pull_deliver_the_published_contract(client):
    response = client.get("/work-contract")
    assert response.status_code == 200
    contract = response.json()
    assert contract["version"] and "TDD" in contract["instructions"]
    assert "실패" in contract["instructions"] and "대체 검증" in contract["instructions"]
    assert contract["instructions"] in client.get("/api.md").text
    claimed = start(client)
    assert claimed["work_contract"] == contract
    assert claimed["execution_attempt"] == 1
    other = client.post("/issues", json={"title": "pull task"}).json()
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

    monkeypatch.setattr(appmod.urllib.request, "urlopen", receive)
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
    assert client.post("/issues", json={"title": "skip", "state": state}).status_code == 422


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
    return {
        "contract_version": issue["work_contract"]["version"],
        "attempt": issue["execution_attempt"],
        "method": "tdd", "red_command": "pytest tests/test_retry.py",
        "red_evidence": "1 failed: expected one notification, observed two",
        "command": "pytest tests/test_retry.py", "result": "passed",
        "evidence": "1 passed; duplicate notification no longer reproduced",
        **fields,
    }


def test_tdd_report_is_recorded_as_reported_evidence(client):
    issue = start(client)
    proof = report(issue)
    response = client.patch(f"/issues/{issue['id']}", json={"state": "done", "completion_report": proof})
    assert response.status_code == 200
    data = response.json()
    assert data["state"] == "done" and data["verification_status"] == "reported"
    assert data["completion_report"]["red_evidence"] == proof["red_evidence"]
    assert data["completion_report"]["evidence"] == proof["evidence"]


@pytest.mark.parametrize("overrides", [
    {"red_command": ""}, {"red_evidence": ""}, {"command": " "},
    {"method": "alternative", "reason": ""},
])
def test_incomplete_report_is_rejected(client, overrides):
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


def test_alternative_report_requires_reason_but_not_red(client):
    issue = start(client)
    proof = report(issue, method="alternative", reason="documentation-only", red_command="", red_evidence="")
    response = client.patch(f"/issues/{issue['id']}", json={"state": "done", "completion_report": proof})
    assert response.status_code == 200 and response.json()["state"] == "done"


def test_contract_mismatch_and_scope_change_reject_stale_report(client):
    issue = start(client)
    path = f"/issues/{issue['id']}"
    assert client.patch(path, json={"state": "done", "completion_report": report(issue, contract_version="old")}).status_code == 409
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
    import app as appmod
    monkeypatch.setenv("TT_REQUIRE_REPORT", "1")
    monkeypatch.setattr(appmod, "DONE_GATE", mode)
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
    issue = strict.post("/issues", json={"title": "existing production work"}).json()
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
