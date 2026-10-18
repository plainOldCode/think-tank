"""3D5T(M568PGM1-3D5T): poll pending_inbox가 종료 카드·이전 회차 queued 행을
배달하지 않는다 — 9PDD 후속.

9PDD는 POST /issues/{id}/dispatch에만 종료 카드 검사를 넣었다. poll 수신
에이전트의 배달 경로(GET /agents/{runner}/pending)는 검사 없이 queued 행을
그대로 배달해 done 카드를 재청구시킬 수 있다(QN0H 부류 재발 경로).
"""
import sqlite3

from conftest import mk

SECRET = "poll-secret-3d5t"


def _reg_poll(client):
    r = client.post("/agents", json={"name": "poller3d5t", "receive_mode": "poll",
                                     "runner_name": "runner-3d5t", "secret": SECRET})
    assert r.status_code in (200, 201), r.text


def _dispatch(client, issue_id):
    r = client.post(f"/issues/{issue_id}/dispatch",
                    json={"agent": "poller3d5t", "message": "3d5t 작업"})
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def _row(client, did):
    con = sqlite3.connect(client.app.state.ctx.db_path)
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT status, attempt FROM dispatches WHERE id=?", (did,)).fetchone()
    con.close()
    return row


def _pending(client):
    return client.get("/agents/runner-3d5t/pending",
                      headers={"authorization": f"Bearer {SECRET}"})


def _mark_done(client, issue_id):
    issue = client.post(f"/issues/{issue_id}/claim", json={"agent": "worker"}).json()
    proof = {
        "contract_version": issue["work_contract"]["version"],
        "attempt": issue["execution_attempt"], "method": "planned", "result": "passed",
        "design": {"criteria": "local acceptance", "verification": "run local tests"},
        "implementation": {"summary": "artifact", "commands": "test"},
        "verification": {"commands": "test", "evidence": "1 passed"},
    }
    r = client.patch(f"/issues/{issue_id}", json={"state": "done", "completion_report": proof})
    assert r.status_code == 200, r.text


def test_종료_카드의_queued_행은_배달되지_않고_소멸된다(client):
    _reg_poll(client)
    issue = mk(client, title="3d5t 종료 카드")
    did = _dispatch(client, issue["id"])
    _mark_done(client, issue["id"])

    assert _pending(client).json() == []
    row = _row(client, did)
    assert row["status"] == "superseded"


def test_이전_회차_queued_행은_배달되지_않고_소멸된다(client):
    _reg_poll(client)
    issue = mk(client, title="3d5t 이전 회차")
    r = client.post(f"/issues/{issue['id']}/claim", json={"agent": "worker"})
    assert r.status_code == 200, r.text           # a1 선점
    did = _dispatch(client, issue["id"])          # a1에 발행
    r = client.patch(f"/issues/{issue['id']}", json={"state": "todo"})
    assert r.status_code == 200, r.text
    r = client.post(f"/issues/{issue['id']}/claim", json={"agent": "worker2"})
    assert r.status_code == 200, r.text           # 회차 전진 a2

    assert _pending(client).json() == []
    row = _row(client, did)
    assert row["attempt"] == 1 and row["status"] == "superseded"


def test_유효_queued_행은_정상_배달된다(client):
    _reg_poll(client)
    issue = mk(client, title="3d5t 유효 카드")
    did = _dispatch(client, issue["id"])

    out = _pending(client)
    assert [d["dispatch_id"] for d in out.json()] == [did]
    assert _row(client, did)["status"] == "ok"    # 이중 전달 차단 마킹 유지
