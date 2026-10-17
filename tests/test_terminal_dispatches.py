"""Terminal issues own dispatch cleanup, independent of runner delivery."""
import json
import sqlite3

import pytest

from conftest import mk
from test_api import make_dispatch, one_dispatch, progress_post, reg_agent


def seed(client, iid, state, attempt=0):
    with sqlite3.connect(client.app.state.db_path) as c:
        return c.execute(
            "INSERT INTO dispatches (issue_id, agent, author, message, status, ts, "
            "run_state, attempt, session) VALUES (?, 'run', 'test', '', 'ok', "
            "'2020-01-01T00:00:00+0900', ?, ?, 'tt-test-1')",
            (iid, state, attempt)).lastrowid


@pytest.mark.parametrize("via", ["cancelled", "archive", "human", "verify", "auto-archive"])
def test_terminal_transition_cascades_all_active_dispatches(client, monkeypatch, via):
    i = mk(client)
    iid = i["id"]
    if via in {"human", "verify"}:
        assert client.patch(f"/issues/{iid}", json={"state": "in_progress"}).status_code == 200
    if via == "verify":
        assert client.patch(f"/issues/{iid}", json={"state": "review"}).status_code == 200
    if via == "auto-archive":
        monkeypatch.setenv("TT_ARCHIVE_AFTER_DAYS", "14")
        with sqlite3.connect(client.app.state.db_path) as c:
            c.execute("UPDATE issues SET state='done', completed_at='2020-01-01' WHERE id=?", (iid,))
    dids = [seed(client, iid, s) for s in ("queued", "running", "stalled")]
    preserved = [seed(client, iid, s) for s in ("finished", "failed", "")]
    other = mk(client)
    other_did = seed(client, other["id"], "running")
    if via in {"human", "verify"}:
        r = client.post(f"/issues/{iid}/verify", json={"verifier": "tester", "human": via == "human",
                                                      "evidence": "pytest 1 passed"})
    elif via == "auto-archive":
        r = client.post(f"/issues/{iid}/archive")
    else:
        payload = {"archived": True} if via == "archive" else {"state": via}
        r = client.patch(f"/issues/{iid}", json=payload)
    assert r.status_code == 200, r.text
    for did in dids:
        row = one_dispatch(client, iid, did)
        assert row["run_state"] == "finished"
        assert row["ended_at"] and row["session"] == "tt-test-1" and row["status"] == "ok"
    assert [one_dispatch(client, iid, d)["run_state"] for d in preserved] == ["finished", "failed", ""]
    assert [d["dispatch_id"] for d in client.get("/agents/active").json()] == [other_did]
    with sqlite3.connect(client.app.state.db_path) as c:
        events = c.execute("SELECT entity_id, payload FROM events WHERE kind='dispatch.updated'").fetchall()
    assert {int(d) for d, p in events if json.loads(p).get("via") == "issue-terminal"} == set(dids)


@pytest.mark.parametrize("closed", ["done", "cancelled", "archived"])
@pytest.mark.parametrize("terminal", ["finished", "failed"])
def test_old_attempt_cleanup_accepts_closed_issue_but_not_old_comment(client, hook_server, closed, terminal):
    i = mk(client)
    reg_agent(client, hook_server)
    did = make_dispatch(client, i["id"])
    with sqlite3.connect(client.app.state.db_path) as c:
        c.execute("UPDATE issues SET execution_attempt=2, state=?, archived=? WHERE id=?",
                  ("todo" if closed == "archived" else closed, int(closed == "archived"), i["id"]))
    payload = {"state": terminal, "comment": "old attempt success", "session": "tt-old-1"}
    assert progress_post(client, i["id"], did, payload, secret="wrong").status_code == 403
    r = progress_post(client, i["id"], did, payload)
    assert r.status_code == 200, r.text
    assert r.json()["run_state"] == terminal
    assert not r.json()["report_session"]
    assert not any(c["body"] == payload["comment"] for c in client.get(f"/issues/{i['id']}").json()["comments"])
    with sqlite3.connect(client.app.state.db_path) as c:
        assert c.execute("SELECT COUNT(*) FROM dispatch_reports WHERE dispatch_id=?", (did,)).fetchone()[0] == 0
        c.execute("UPDATE issues SET state='in_progress', archived=0 WHERE id=?", (i["id"],))
    # 재개 후에도 구회차 종료는 투영만 갱신한다. 코멘트·접수 이력은 생기지 않는다.
    reopened = progress_post(client, i["id"], did, payload)
    assert reopened.status_code == 200, reopened.text
    assert reopened.json()["run_state"] == terminal and not reopened.json()["report_session"]
    assert not any(c["body"] == payload["comment"] for c in client.get(f"/issues/{i['id']}").json()["comments"])
    with sqlite3.connect(client.app.state.db_path) as c:
        assert c.execute("SELECT COUNT(*) FROM dispatch_reports WHERE dispatch_id=?", (did,)).fetchone()[0] == 0


@pytest.mark.parametrize("state", ["queued", "running", "stalled"])
def test_late_active_progress_cannot_resurrect_closed_issue(client, state):
    i = mk(client)
    did = seed(client, i["id"], "running")
    assert client.patch(f"/issues/{i['id']}", json={"state": "cancelled"}).status_code == 200
    r = progress_post(client, i["id"], did, {"state": state}, secret=None)
    assert r.status_code == 200, r.text
    assert r.json()["run_state"] == "finished"
    assert client.get("/agents/active").json() == []


def test_old_accepted_retry_preserves_reopened_projection(client, hook_server):
    i = mk(client)
    reg_agent(client, hook_server)
    did = make_dispatch(client, i["id"])
    payload = {"state": "finished", "session": "tt-old-1", "comment": "accepted old report"}
    assert progress_post(client, i["id"], did, payload).status_code == 200
    assert client.patch(f"/issues/{i['id']}", json={"state": "in_progress"}).status_code == 200
    assert progress_post(client, i["id"], did, {"state": "running", "session": "tt-new-2"}).status_code == 200
    r = progress_post(client, i["id"], did, payload)
    assert r.status_code == 200 and r.json()["run_state"] == "running"
    assert r.json()["session"] == "tt-new-2"
    comments = client.get(f"/issues/{i['id']}").json()["comments"]
    assert sum(c["body"] == payload["comment"] for c in comments) == 1


def test_issue_conflict_rolls_back_cleanup(client):
    i = mk(client)
    did = seed(client, i["id"], "running")
    r = client.patch(f"/issues/{i['id']}", json={"state": "cancelled", "expected_version": 999})
    assert r.status_code == 409
    assert one_dispatch(client, i["id"], did)["run_state"] == "running"
    with sqlite3.connect(client.app.state.db_path) as c:
        assert c.execute("SELECT COUNT(*) FROM events WHERE kind='dispatch.updated'").fetchone()[0] == 0
