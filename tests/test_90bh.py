"""90BH(M568PH4D-90BH): 제출 시 제출자 소유 poll dispatch 행 finished — 3A0B 후속.

review-fix를 받은 poll 에이전트가 재제출해도 그 행이 running으로 남아 probe의
같은 에이전트 재발령 가드(_occupies_card)에 걸린다. 제출(리뷰 진입) 시점에
제출자 소유 poll 행만 닫고, 리뷰어 등 타 에이전트 행은 유지한다.
"""
import sqlite3

from conftest import mk
import probe.core
from probe.core import _occupies_card

SECRET = "poll-secret-90bh"


def _reg_poll(client, name="poller90bh"):
    r = client.post("/agents", json={"name": name, "receive_mode": "poll",
                                     "runner_name": "runner-90bh", "secret": SECRET})
    assert r.status_code in (200, 201), r.text


def _dispatch(client, issue_id, agent):
    r = client.post(f"/issues/{issue_id}/dispatch", json={"agent": agent, "message": "90bh"})
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def _pickup(client, did):
    """poll 러너 픽업 시뮬레이션 — run_state=running."""
    con = sqlite3.connect(client.app.state.ctx.db_path)
    con.execute("UPDATE dispatches SET status='ok', run_state='running', "
                "started_at=datetime('now') WHERE id=?", (did,))
    con.commit()
    con.close()


def _row(client, did):
    con = sqlite3.connect(client.app.state.ctx.db_path)
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT run_state, status FROM dispatches WHERE id=?", (did,)).fetchone()
    con.close()
    return dict(row)


def _submit(client, issue_id):
    issue = client.get(f"/issues/{issue_id}").json()
    proof = {
        "contract_version": issue["work_contract"]["version"],
        "attempt": issue["execution_attempt"], "method": "planned", "result": "passed",
        "design": {"criteria": "local acceptance", "verification": "run local tests"},
        "implementation": {"summary": "artifact", "commands": "test"},
        "verification": {"commands": "test", "evidence": "1 passed"},
    }
    r = client.patch(f"/issues/{issue_id}", json={"state": "done", "completion_report": proof})
    assert r.status_code == 200, r.text
    return client.get(f"/issues/{issue_id}").json()


def test_제출시_제출자_poll_행은_finished가_된다(client):
    _reg_poll(client)
    issue = mk(client, title="90bh 제출 종결")
    client.post(f"/issues/{issue['id']}/claim", json={"agent": "poller90bh"})
    did = _dispatch(client, issue["id"], "poller90bh")
    _pickup(client, did)

    card = _submit(client, issue["id"])
    assert card["state"] == "review"
    assert _row(client, did)["run_state"] == "finished"


def test_리뷰어_등_타_에이전트_행은_유지된다(client):
    _reg_poll(client)
    _reg_poll(client, "poller-rev")
    issue = mk(client, title="90bh 타 에이전트")
    client.post(f"/issues/{issue['id']}/claim", json={"agent": "poller90bh"})
    did_w = _dispatch(client, issue["id"], "poller90bh")
    did_r = _dispatch(client, issue["id"], "poller-rev")
    _pickup(client, did_w)
    _pickup(client, did_r)

    _submit(client, issue["id"])
    assert _row(client, did_w)["run_state"] == "finished"
    assert _row(client, did_r)["run_state"] == "running"  # 리뷰어 행 유지


def test_제출후_같은_에이전트_재발령_가드는_막지_않는다(client):
    _reg_poll(client, "poller90bh")  # 기본 이름 명시 — 리뷰어 폴링러와 독립
    assert client.post("/agents", json={"name": "poller90bh", "receive_mode": "poll",
                                        "runner_name": "runner-90bh",
                                        "secret": SECRET}).status_code in (200, 201, 409)
    issue = mk(client, title="90bh 재발령")
    client.post(f"/issues/{issue['id']}/claim", json={"agent": "poller90bh"})
    did = _dispatch(client, issue["id"], "poller90bh")
    _pickup(client, did)
    _submit(client, issue["id"])

    con = sqlite3.connect(client.app.state.ctx.db_path)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(
        "SELECT * FROM dispatches WHERE issue_id=?", (issue["id"],)).fetchall()]
    con.close()
    card = client.get(f"/issues/{issue['id']}").json()
    assert not any(_occupies_card(r, card["execution_attempt"]) and r.get("agent") == "poller90bh"
                   for r in rows)


def test_미픽업_행은_제출시_배달_큐에서도_빠진다(client):
    # 3D5T RC — run_state만 finished면 다음 폴링에 status=queued 행이 되살아난다
    _reg_poll(client)
    issue = mk(client, title="90bh 미픽업 제출")
    client.post(f"/issues/{issue['id']}/claim", json={"agent": "poller90bh"})
    did = _dispatch(client, issue["id"], "poller90bh")   # 픽업 없이 제출

    _submit(client, issue["id"])
    row = _row(client, did)
    assert row["run_state"] == "finished" and row["status"] == "superseded"
    out = client.get("/agents/runner-90bh/pending",
                     headers={"authorization": f"Bearer {SECRET}"})
    assert out.json() == []                              # 재배달 없음
    assert _row(client, did)["run_state"] == "finished"  # 되살아나지 않음
