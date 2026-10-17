"""3A0B: poll 수신 에이전트 dispatch 생명주기 추적.

운영 실측(2026-10-17): opencode-tp13 poll dispatch 7건 전부 run_state=''로 남아
/agents/active·probe busy·stall 감시·지표가 전부 못 봤다. poll 발송 시점부터
픽업·종료까지 run_state로 추적 가능해야 한다.
"""
import sqlite3

from conftest import mk, Hook

SECRET = "poll-secret-1"


def _reg_poll(client):
    r = client.post("/agents", json={"name": "poller", "receive_mode": "poll",
                                     "runner_name": "runner-a", "secret": SECRET})
    assert r.status_code in (200, 201), r.text


def _dispatch(client, issue_id, msg="poll 작업"):
    r = client.post(f"/issues/{issue_id}/dispatch", json={"agent": "poller", "message": msg})
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def _active(client):
    return client.get("/agents/active").json()


def _row(client, did):
    con = sqlite3.connect(client.app.state.ctx.db_path)
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT * FROM dispatches WHERE id=?", (did,)).fetchone()
    con.close()
    return row


def test_poll_dispatch는_발송_시점부터_active로_보인다(client):
    _reg_poll(client)
    issue = mk(client, title="poll 가시성")
    did = _dispatch(client, issue["id"])

    acts = _active(client)
    assert any(a["dispatch_id"] == did for a in acts), acts
    row = _row(client, did)
    assert row["run_state"] == "queued" and row["status"] == "queued"


def test_poll_픽업은_running_started_at과_이벤트를_남긴다(client):
    _reg_poll(client)
    issue = mk(client, title="poll 픽업")
    did = _dispatch(client, issue["id"])

    r = client.get("/agents/runner-a/pending",
                   headers={"authorization": f"Bearer {SECRET}"})
    assert r.status_code == 200, r.text
    assert len(r.json()) == 1

    row = _row(client, did)
    assert row["status"] == "ok"            # 이중 전달 차단 마킹
    assert row["run_state"] == "running"    # 러너가 작업 보유
    assert row["started_at"] is not None

    con = sqlite3.connect(client.app.state.ctx.db_path)
    ev = con.execute("SELECT count(*) n FROM events WHERE kind='dispatch.updated' "
                     "AND entity_id=? AND json_extract(payload,'$.run_state')='running'",
                     (str(did),)).fetchone()[0]
    con.close()
    assert ev >= 1


def test_poll_행도_카드_종료에_finished로_닫힌다(client):
    _reg_poll(client)
    issue = mk(client, title="poll 종료")
    did = _dispatch(client, issue["id"])
    client.get("/agents/runner-a/pending", headers={"authorization": f"Bearer {SECRET}"})
    assert _row(client, did)["run_state"] == "running"

    r = client.patch(f"/issues/{issue['id']}", json={"state": "cancelled"})
    assert r.status_code == 200, r.text

    row = _row(client, did)
    assert row["run_state"] == "finished" and row["ended_at"] is not None


def test_probe_점유_판정은_poll_행을_현재_회차_활성으로_센다(client):
    from probe.core import _occupies_card
    assert _occupies_card({"run_state": "queued", "attempt": 2}, 2)
    assert _occupies_card({"run_state": "running", "attempt": 2}, 2)
    assert not _occupies_card({"run_state": "finished", "attempt": 2}, 2)


def test_probe_활성_집합은_config_집합을_포괄한다():
    """활성 정의가 흩어져 있는 문제(카드 제안 4) — 최소 계약: probe 집합 ⊇ config."""
    import config
    import probe.core as core
    assert set(config.ACTIVE_RUN_STATES) <= core._ACTIVE_RUN_STATES
