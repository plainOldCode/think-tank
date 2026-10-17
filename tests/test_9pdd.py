"""9PDD: 완료 카드·이전 회차 dispatch 재전달 차단 + 종료 시 error/queued 행 정리.

운영 사건(M54AGANS-QN0H, 2026-10-17): 낡은 error 행의 재전달이 done 카드를
재청구시켰다. 재전달 경로는 카드 상태와 회차를 보지 않고, 카드 종료·회차
전진 시 status(error/queued) 행은 정리되지 않아 재전달 대상으로 남았다.
"""
import sqlite3

import pytest

from conftest import mk, Hook


def _mark_done(client, issue_id):
    """기존 카드를 in_progress→done으로 전이(완료 보고서 포함)."""
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
    return client.get(f"/issues/{issue_id}").json()


def _mk_errored_row(client, title, msg="재전달 대상"):
    """dispatch 1건 만들고 error로 강제."""
    issue = mk(client, title=title)
    r = client.post(f"/issues/{issue['id']}/dispatch", json={"agent": "demo", "message": msg})
    assert r.status_code in (200, 201), r.text
    did = r.json()["id"]
    con = sqlite3.connect(client.app.state.ctx.db_path)
    con.execute("UPDATE dispatches SET status='error', detail='HTTP 401' WHERE id=?", (did,))
    con.commit()
    con.close()
    return issue, did


def _row_status(client, did):
    con = sqlite3.connect(client.app.state.ctx.db_path)
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT status, attempt FROM dispatches WHERE id=?", (did,)).fetchone()
    con.close()
    return row


def test_done_카드의_error_행_재전달은_차단된다(client, hook_server, monkeypatch):
    import probe.core as core
    from test_tt_lifecycle import approve, snap

    client.post("/agents", json={"name": "demo", "base_url": hook_server})
    payload = {"agent": "demo", "message": "재전달 대상"}
    issue = mk(client, title="done 재전달 차단")
    issue = client.post(f"/issues/{issue['id']}/claim", json={"agent": "worker"}).json()
    did = client.post(f"/issues/{issue['id']}/dispatch", json=payload).json()["id"]
    con = sqlite3.connect(client.app.state.ctx.db_path)
    con.execute("UPDATE dispatches SET status='error', detail='HTTP 401' WHERE id=?", (did,))
    con.commit()
    con.close()
    Hook.received.clear()

    # 정상 done 경로 — 제출(review 전이)→승인→verify(완료 보고서 게이트)
    r = client.patch(f"/issues/{issue['id']}", json={
        "state": "done",
        "completion_report": {"contract_version": issue["work_contract"]["version"],
                              "attempt": issue["execution_attempt"], "method": "planned",
                              "result": "passed",
                              "design": {"criteria": "local", "verification": "run local tests"},
                              "implementation": {"summary": "artifact", "commands": "test"},
                              "verification": {"commands": "test", "evidence": "1 passed"}}})
    assert r.status_code == 200, r.text
    issue = approve(client, client.get(f"/issues/{issue['id']}").json())
    acts = core.decide(snap(issue))
    assert acts[0]["action"] == "tt-verify"

    def api(url, path, method="GET", body=None):
        resp = client.request(method, path, json=body)
        assert resp.status_code < 400, resp.text
        return resp.json()

    monkeypatch.setattr(core, "api", api)
    core.execute("http://tt", acts[0])
    got = client.get(f"/issues/{issue['id']}").json()
    assert got["state"] == "done"
    a_final = got["execution_attempt"]

    # 동일 회차 error 행은 done 전이 시점에 이미 superseded — 재전송도 종료 카드라 409
    r = client.post(f"/issues/{issue['id']}/dispatch", json=payload)
    assert r.status_code == 409, r.text
    assert len(Hook.received) == 0  # 웹훅 없음 — 카드 재청구 없음
    assert _row_status(client, did)["status"] == "superseded"
    # 다른 본문 신규 dispatch도 종료 카드라면 409 — 멱등키 우회 차단(QN0H 경로)
    r2 = client.post(f"/issues/{issue['id']}/dispatch", json={"agent": "demo", "message": "다른 지시"})
    assert r2.status_code == 409, r2.text
    assert len(Hook.received) == 0
    got = client.get(f"/issues/{issue['id']}").json()
    assert got["state"] == "done" and got["execution_attempt"] == a_final


def test_이전_회차_error_행_재전달은_차단된다(client, hook_server):
    client.post("/agents", json={"name": "demo", "base_url": hook_server})
    payload = {"agent": "demo", "message": "이전 회차 재전달"}
    issue, did = _mk_errored_row(client, "회차 재전달 차단")
    Hook.received.clear()
    client.post(f"/issues/{issue['id']}/claim", json={"agent": "worker"})  # a1→a2 전진

    assert _row_status(client, did)["status"] == "superseded"
    r = client.post(f"/issues/{issue['id']}/dispatch", json=payload)
    assert r.status_code in (200, 201)  # 새 회차 신규 dispatch는 정상
    assert _row_status(client, did)["status"] == "superseded"  # 낡은 행은 그대로 닫힘


def test_카드_종료_시_error_queued_행이_닫힌다(client, hook_server):
    client.post("/agents", json={"name": "demo", "base_url": hook_server})
    issue = mk(client, title="종료 정리")
    ids = []
    for msg in ("첫 지시", "둘째 지시"):
        ids.append(client.post(f"/issues/{issue['id']}/dispatch",
                               json={"agent": "demo", "message": msg}).json()["id"])
    con = sqlite3.connect(client.app.state.ctx.db_path)
    con.execute("UPDATE dispatches SET status='error' WHERE id=?", (ids[0],))
    con.execute("UPDATE dispatches SET status='queued', detail='' WHERE id=?", (ids[1],))
    con.commit()
    con.close()
    Hook.received.clear()
    _mark_done(client, issue["id"])

    for did in ids:
        assert _row_status(client, did)["status"] == "superseded"
    assert len(Hook.received) == 0
    # 종료 정리는 이벤트를 남긴다
    con = sqlite3.connect(client.app.state.ctx.db_path)
    con.row_factory = sqlite3.Row
    ev = con.execute("SELECT count(*) n FROM events WHERE kind='dispatch.updated' "
                     "AND entity_id=? AND json_extract(payload,'$.status')='superseded'",
                     (str(ids[0]),)).fetchone()["n"]
    con.close()
    assert ev >= 1


def test_회차_전진_시_error_queued_행이_닫힌다(client, hook_server):
    client.post("/agents", json={"name": "demo", "base_url": hook_server})
    issue, did = _mk_errored_row(client, "회차 정리")

    r = client.post(f"/issues/{issue['id']}/claim", json={"agent": "worker"})
    assert r.status_code == 200, r.text  # a1→a2 전진
    assert _row_status(client, did)["status"] == "superseded"


def test_활성_카드_속성_수정은_error_queued_행을_닫지_않는다(client, hook_server):
    """a1 리뷰 R1 회귀 — finish_issue_dispatches의 status 정리에 종료 가드가 없으면
    활성 카드 PATCH마다 재전달 대기 error 행·poll queued 행이 조기 종료됐다."""
    client.post("/agents", json={"name": "demo", "base_url": hook_server})
    issue, did = _mk_errored_row(client, "활성 카드 정리 금지")
    client.post("/agents", json={"name": "poller", "receive_mode": "poll", "runner_name": "opencode-tp13"})
    r = client.post(f"/issues/{issue['id']}/dispatch",
                    json={"agent": "poller", "message": "poll 대기"})
    assert r.status_code in (200, 201), r.text
    poll_did = r.json()["id"]

    r = client.patch(f"/issues/{issue['id']}", json={"priority": 2})
    assert r.status_code == 200, r.text

    assert _row_status(client, did)["status"] == "error"      # 재전달 대기 유지
    assert _row_status(client, poll_did)["status"] == "queued"  # poll 픽업 대기 유지
