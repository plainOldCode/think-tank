"""M5CTMZH0-2NHN — 게이트 보류 가시성·승인 해제 경로·게이트 스코프(서버 측).

운영 실측: PR#202 리뷰 발령 #673이 GATE-HOLD(rm -f 패턴) 후 [tt-review-stalled]로
오판됐다. 원인 셋 — ① held 투영 부재 ② held run_state 미수용 ③ brief 첨부로
'승인' 정확 일치 불가. 서버 계약: held는 투영되고 수용되며, 종료 카드에서 닫히고,
dispatch payload은 첨부 전 원본 지시문(instruction)을 실어준다.
"""
import sqlite3

from conftest import mk

SECRET = "gate-secret-1"


def _reg(client):
    r = client.post("/agents", json={"name": "gater", "receive_mode": "poll",
                                     "runner_name": "runner-a", "secret": SECRET})
    assert r.status_code in (200, 201), r.text


def _dispatch(client, issue_id, msg="원본 지시문"):
    r = client.post(f"/issues/{issue_id}/dispatch", json={"agent": "gater", "message": msg})
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def _row(client, did):
    con = sqlite3.connect(client.app.state.ctx.db_path)
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT * FROM dispatches WHERE id=?", (did,)).fetchone()
    con.close()
    return row


def _held(client, issue_id, did):
    r = client.post(f"/issues/{issue_id}/dispatches/{did}/progress",
                    json={"state": "held"}, headers={"authorization": f"Bearer {SECRET}"})
    assert r.status_code == 200, r.text
    return r


def test_held_progress는_200이고_active에는_안_보인다(client):
    _reg(client)
    issue = mk(client)
    did = _dispatch(client, issue["id"])

    _held(client, issue["id"], did)

    row = _row(client, did)
    assert row["run_state"] == "held"
    acts = client.get("/agents/active").json()
    assert not any(a["dispatch_id"] == did for a in acts)  # 활성 정의 불변
    # probe 점유 판정 — held 행은 카드를 점유한다(재시도·stalled 오판 방지)
    con = sqlite3.connect(client.app.state.ctx.db_path)
    ea = con.execute("SELECT execution_attempt FROM issues WHERE id=?", (issue["id"],)).fetchone()[0]
    con.close()
    from probe.core import _occupies_card
    assert _occupies_card(dict(row), ea)


def test_dispatch_payload은_instruction_원본과_첨부본을_구분한다(client):
    _reg(client)
    issue = mk(client)
    con = sqlite3.connect(client.app.state.ctx.db_path)
    con.execute("UPDATE issues SET brief_verify='pytest -q tests/' WHERE id=?",
                (issue["id"],))
    con.commit()
    con.close()

    did = _dispatch(client, issue["id"], "폴더를 정리하라")
    r = client.get("/agents/runner-a/pending", headers={"authorization": f"Bearer {SECRET}"})
    assert r.status_code == 200, r.text
    payload = r.json()[0]
    # 에이전트 전달 본문은 첨부 포함(기존 계약 유지)
    assert "[brief]" in payload["message"] and "pytest -q" in payload["message"]
    # 판정 대상은 원본뿐 — brief의 rm -f가 게이트를 건드리지 않는다
    assert payload["instruction"] == "폴더를 정리하라"


def test_dispatch_payload은_첨부가_없으면_원본_그대로다(client):
    _reg(client)
    issue = mk(client)
    did = _dispatch(client, issue["id"], "그냥 작업")
    r = client.get("/agents/runner-a/pending", headers={"authorization": f"Bearer {SECRET}"})
    payload = r.json()[0]
    assert payload["instruction"] == "그냥 작업"
    assert payload["message"] == "그냥 작업"


def test_held_행도_카드_종료에_finished로_닫힌다(client):
    _reg(client)
    issue = mk(client)
    did = _dispatch(client, issue["id"])
    _held(client, issue["id"], did)
    assert _row(client, did)["run_state"] == "held"

    r = client.patch(f"/issues/{issue['id']}", json={"state": "cancelled"})
    assert r.status_code == 200, r.text

    row = _row(client, did)
    assert row["run_state"] == "finished" and row["ended_at"] is not None


def test_held_리뷰어는_재발령도_stalled도_내지_않는다(client):
    from probe.tt_lifecycle import plan

    class _Core:
        NON_EXECUTORS = set()

        def _resolve_executor(self, assignee, agents):
            return assignee

    issue = {"id": "TT-1", "state": "review", "title": "리뷰 중",
             "assignee": "rev", "busy_agents": ["rev"],  # held 투영이 만든 점유
             "comments": [], "events": [],
             "labels": [], "body": "완료 기준: 리뷰"}
    actions = plan(_Core(), issue, [], {"rev"}, "2026-10-21T12:00:00")
    assert actions == []
