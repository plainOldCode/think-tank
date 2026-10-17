"""M54E72RQ-SXGS — an old-round dispatch must be closable and must not block review.

The attempt check still drops old-round completion comments and report receipts.
It must not leave a killed/timed-out dispatch looking active, and the probe must
not treat that leftover (or any terminal row) as a reason to skip reviewer assignment.
"""
import sqlite3

import pytest

import probe.core as core
from conftest import mk
from test_api import make_dispatch, progress_post, reg_agent
from test_tt_lifecycle import snap, submitted


def _seed(client, iid, state, attempt, agent="leftover", session="tt-old"):
    with sqlite3.connect(client.app.state.db_path) as c:
        return c.execute(
            "INSERT INTO dispatches (issue_id, agent, author, message, status, ts, "
            "run_state, attempt, session) VALUES (?, ?, 'test', '', 'ok', "
            "'2020-01-01T00:00:00+0900', ?, ?, ?)",
            (iid, agent, state, attempt, session)).lastrowid


def _row(client, did):
    with sqlite3.connect(client.app.state.db_path) as c:
        c.row_factory = sqlite3.Row
        return dict(c.execute("SELECT * FROM dispatches WHERE id=?", (did,)).fetchone())


def _reports(client, did):
    with sqlite3.connect(client.app.state.db_path) as c:
        return c.execute(
            "SELECT COUNT(*) FROM dispatch_reports WHERE dispatch_id=?", (did,)).fetchone()[0]


def _bodies(client, iid):
    return [c["body"] for c in client.get(f"/issues/{iid}").json()["comments"]]


def _bump_attempt(client, iid, attempt):
    with sqlite3.connect(client.app.state.db_path) as c:
        c.execute("UPDATE issues SET execution_attempt=? WHERE id=?", (attempt, iid))


def _api(client):
    def api(url, path, method="GET", body=None):
        r = client.request(method, path, json=body)
        assert r.status_code < 400, r.text
        return r.json()

    return api


@pytest.mark.parametrize("state", ["finished", "failed"])
@pytest.mark.parametrize("with_comment", [False, True])
def test_old_round_terminal_close_does_not_count_as_evidence(client, hook_server, state, with_comment):
    """killed/timeout (failed, no comment), failed, and done (finished) close the
    old dispatch. A completion comment on that report is not current-round evidence."""
    i = mk(client, title="stale close")
    reg_agent(client, hook_server)
    did = make_dispatch(client, i["id"])
    assert progress_post(client, i["id"], did, {"state": "running", "session": "tt-old"}).status_code == 200
    _bump_attempt(client, i["id"], 2)
    before = _bodies(client, i["id"])
    evidence = "runner:test dispatch#%d done exit=0 pytest 1 passed session=tt-old" % did
    payload = {"state": state, "session": "tt-old"}
    if with_comment:
        payload.update(comment=evidence, author="runner@t")
    r = progress_post(client, i["id"], did, payload)
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["run_state"] == state
    assert got["ended_at"]
    assert not got["report_session"]
    assert evidence not in _bodies(client, i["id"])
    assert _bodies(client, i["id"]) == before
    assert _reports(client, did) == 0
    issue = client.get(f"/issues/{i['id']}").json()
    assert issue["verification_status"] == "unverified"
    assert not issue["completion_report"]
    assert [d["dispatch_id"] for d in client.get("/agents/active").json()] == []
    # a retry still does not become evidence
    assert progress_post(client, i["id"], did, payload).status_code == 200
    assert evidence not in _bodies(client, i["id"])
    assert _reports(client, did) == 0


def test_stale_kill_does_not_clobber_a_newer_session(client, hook_server):
    """A previously started session on the same row keeps its projection when an
    older session's kill arrives. Evidence is still not recorded."""
    i = mk(client, title="newer session")
    reg_agent(client, hook_server)
    did = make_dispatch(client, i["id"])
    assert progress_post(client, i["id"], did,
                         {"state": "running", "session": "tt-old"}).status_code == 200
    _bump_attempt(client, i["id"], 2)
    assert progress_post(client, i["id"], did,
                         {"state": "running", "session": "tt-new"}).status_code == 200
    r = progress_post(client, i["id"], did, {"state": "failed", "session": "tt-old"})
    assert r.status_code == 200, r.text
    assert r.json()["run_state"] == "running"
    assert r.json()["session"] == "tt-new"
    assert _reports(client, did) == 0


def test_current_round_terminal_comment_still_counts(client, hook_server):
    i = mk(client, title="current evidence")
    reg_agent(client, hook_server)
    did = make_dispatch(client, i["id"])
    body = "runner:test dispatch#%d done exit=0 pytest 1 passed" % did
    r = progress_post(client, i["id"], did, {"state": "finished", "comment": body,
                                             "author": "runner@t", "session": "tt-cur"})
    assert r.status_code == 200, r.text
    assert r.json()["report_session"] == "tt-cur"
    assert body in _bodies(client, i["id"])
    assert _reports(client, did) == 1


@pytest.mark.parametrize("state", ["queued", "running", "stalled"])
def test_rework_claim_leaves_no_blocking_old_round_dispatch(client, hook_server, state):
    issue = submitted(client)
    did = _seed(client, issue["id"], state, issue["execution_attempt"])
    assert client.patch(f"/issues/{issue['id']}", json={"state": "todo"}).status_code == 200
    assert _row(client, did)["run_state"] == state  # same round until the new claim
    claimed = client.post(f"/issues/{issue['id']}/claim", json={"agent": "next-worker"})
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["execution_attempt"] == issue["execution_attempt"] + 1
    row = _row(client, did)
    assert row["run_state"] == "finished"
    assert row["ended_at"] and not row["report_session"]
    assert _reports(client, did) == 0
    assert [d["dispatch_id"] for d in client.get("/agents/active").json()] == []


def test_resubmit_leaves_no_blocking_old_round_dispatch(client, hook_server):
    issue = submitted(client)
    did = _seed(client, issue["id"], "running", issue["execution_attempt"] - 1)
    same = _seed(client, issue["id"], "stalled", issue["execution_attempt"],
                 agent="current", session="tt-current")
    r = client.patch(f"/issues/{issue['id']}", json={
        "state": "done", "completion_report": issue["completion_report"],
    })
    assert r.status_code == 200, r.text
    assert r.json()["execution_attempt"] == issue["execution_attempt"]
    assert r.json()["state"] == "review"
    assert _row(client, did)["run_state"] == "finished"
    assert _row(client, same)["run_state"] == "stalled"  # current round still active
    assert _reports(client, did) == 0


def test_pull_leaves_no_blocking_old_round_dispatch(client, hook_server):
    i = mk(client, title="expired")
    assert client.post(f"/issues/{i['id']}/claim", json={"agent": "crashed@x", "hours": 1}).status_code == 200
    reg_agent(client, hook_server)
    did = make_dispatch(client, i["id"])
    assert progress_post(client, i["id"], did, {"state": "stalled", "session": "tt-old"}).status_code == 200
    with sqlite3.connect(client.app.state.db_path) as c:
        c.execute("UPDATE issues SET lease_expires='2020-01-01T00:00:00+0900' WHERE id=?", (i["id"],))
    got = client.post("/pull", json={"agent": "rescuer"}).json()
    assert got["id"] == i["id"]
    assert got["execution_attempt"] == 2
    row = _row(client, did)
    assert row["run_state"] == "finished" and row["ended_at"]
    assert not row["report_session"] and _reports(client, did) == 0


def test_snapshot_review_ignores_old_round_and_terminal_dispatch(client, hook_server, monkeypatch):
    """Already-stuck production rows heal on the probe's dispatch read: an old-round
    running/stalled dispatch and a terminal one do not block reviewer assignment."""
    monkeypatch.setenv("TT_AUTO_DISPATCH", "1")
    monkeypatch.setenv("TT_REVIEW_GRACE_MIN", "0")
    issue = submitted(client)
    assert client.post("/agents", json={
        "name": "codex", "base_url": hook_server, "receive_mode": "hook",
    }).status_code == 201
    old = _seed(client, issue["id"], "stalled", issue["execution_attempt"] - 1, session="tt-553")
    terminal = _seed(client, issue["id"], "finished", issue["execution_attempt"],
                     agent="run", session="tt-done")
    monkeypatch.setattr(core, "api", _api(client))
    card = next(i for i in core.snapshot("http://tt")["issues"] if i["id"] == issue["id"])
    assert card["busy_agents"] == []
    assert _row(client, old)["run_state"] == "finished"
    assert _row(client, terminal)["run_state"] == "finished"
    acts = core.decide(core.snapshot("http://tt"))
    assert any(a["action"] == "tt-review" and a["issue"] == issue["id"] for a in acts)


def test_execute_assigns_reviewer_despite_old_round_dispatch(client, hook_server, monkeypatch):
    issue = submitted(client)
    assert client.post("/agents", json={
        "name": "codex", "base_url": hook_server, "receive_mode": "hook",
    }).status_code == 201

    def api(url, path, method="GET", body=None):
        if method == "GET" and path.endswith("/dispatches"):
            return [
                {"id": 551, "agent": "leftover", "run_state": "running",
                 "attempt": issue["execution_attempt"] - 1},
                {"id": 552, "agent": "run", "run_state": "failed",
                 "attempt": issue["execution_attempt"]},
            ]
        return _api(client)(url, path, method, body)

    monkeypatch.setattr(core, "api", api)
    act = core.decide(snap(issue))[0]
    assert act["action"] == "tt-review"
    core.execute("http://tt", act)
    got = client.get(f"/issues/{issue['id']}").json()
    assert got["reviewer"] == "codex"
    assert any(d["agent"] == "codex" for d in client.get(f"/issues/{issue['id']}/dispatches").json())


def test_current_round_active_dispatch_still_blocks_review(client, hook_server, monkeypatch):
    monkeypatch.setenv("TT_AUTO_DISPATCH", "1")
    monkeypatch.setenv("TT_REVIEW_GRACE_MIN", "0")
    issue = submitted(client)
    assert client.post("/agents", json={
        "name": "codex", "base_url": hook_server, "receive_mode": "hook",
    }).status_code == 201
    did = _seed(client, issue["id"], "running", issue["execution_attempt"],
                agent="run", session="tt-live")
    monkeypatch.setattr(core, "api", _api(client))
    card = next(i for i in core.snapshot("http://tt")["issues"] if i["id"] == issue["id"])
    assert card["busy_agents"] == ["run"]
    assert _row(client, did)["run_state"] == "running"
    assert not any(a["action"] == "tt-review" and a["issue"] == issue["id"]
                   for a in core.decide(core.snapshot("http://tt")))

    def api(url, path, method="GET", body=None):
        if method == "GET" and path.endswith("/dispatches"):
            return [{"id": did, "agent": "run", "run_state": "stalled",
                     "attempt": issue["execution_attempt"]}]
        return _api(client)(url, path, method, body)

    monkeypatch.setattr(core, "api", api)
    act = core.decide(snap(issue))[0]
    core.execute("http://tt", act)
    assert client.get(f"/issues/{issue['id']}").json()["reviewer"] in (None, "")


def test_null_attempt_active_dispatch_still_blocks(client, hook_server, monkeypatch):
    """Legacy rows with no attempt snapshot keep the historical busy behavior."""
    monkeypatch.setenv("TT_AUTO_DISPATCH", "1")
    issue = submitted(client)
    assert client.post("/agents", json={
        "name": "codex", "base_url": hook_server, "receive_mode": "hook",
    }).status_code == 201
    did = _seed(client, issue["id"], "running", None, agent="run", session="tt-legacy")
    monkeypatch.setattr(core, "api", _api(client))
    card = next(i for i in core.snapshot("http://tt")["issues"] if i["id"] == issue["id"])
    assert card["busy_agents"] == ["run"]
    assert _row(client, did)["run_state"] == "running"
