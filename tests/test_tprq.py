"""TPRQ: hook dispatch 실패 코멘트 폭주 억제.

운영 실측(2026-10-17): 30초 재시도 간격 × 실패마다 시스템 댓글 → W098 카드
댓글 79개 중 38개가 경고. 같은 did의 재시도는 첫 실패만 댓글로 남기고, 이후는
행의 fail_count·detail로 추적한다. 재전달은 지수 backoff(30→60→… 상한 600s),
5회 연속 실패 시 사람 통보 1회.
"""
import sqlite3
from datetime import datetime

from conftest import mk, Hook

DEAD_HOOK = "http://127.0.0.1:1/hook"  # 연결 거부 — #562 사건과 동일 형태


def _backdate(client, did, minutes=10):
    con = sqlite3.connect(client.app.state.ctx.db_path)
    old = "2020-01-01T00:00:00+0900" if minutes is None else (
        datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z"))
    con.execute("UPDATE dispatches SET last_fail_at=? WHERE id=?",
                ("2020-01-01T00:00:00+0900", did))
    con.commit()
    con.close()


def _fail_comments(client, issue_id):
    return [c for c in client.get(f"/issues/{issue_id}").json()["comments"]
            if c["author"] == "tt-server" and "hook dispatch" in c["body"]]


def _row(client, did):
    con = sqlite3.connect(client.app.state.ctx.db_path)
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT * FROM dispatches WHERE id=?", (did,)).fetchone()
    con.close()
    return row


def test_연속_실패해도_실패_댓글은_1건이다(client):
    client.post("/agents", json={"name": "demo", "base_url": DEAD_HOOK})
    issue = mk(client, title="폭주 억제")
    d1 = client.post(f"/issues/{issue['id']}/dispatch",
                     json={"agent": "demo", "message": "메시지"}).json()
    assert d1["status"] == "error"
    assert len(_fail_comments(client, issue["id"])) == 1

    # 재시도 1: backoff 간격 안 → 기수락 200, 웹훅 시도 없음
    d2 = client.post(f"/issues/{issue['id']}/dispatch",
                     json={"agent": "demo", "message": "메시지"})
    assert d2.status_code == 200 and d2.json()["status"] == "error"
    # 재시도 2: backoff 경과 → 재전달 → 또 실패하지만 댓글은 그대로 1건
    _backdate(client, d1["id"])
    d3 = client.post(f"/issues/{issue['id']}/dispatch",
                     json={"agent": "demo", "message": "메시지"}).json()
    assert d3["status"] == "error" and d3["id"] == d1["id"]
    assert _row(client, d1["id"])["fail_count"] == 2
    assert len(_fail_comments(client, issue["id"])) == 1


def test_backoff_간격_안의_재전달은_웹훅을_치지_않는다(client, hook_server):
    """첫 실패 직후(간격 안) 재전송 — 성공 훅이어도 호출 자체가 없어야 한다."""
    client.post("/agents", json={"name": "demo", "base_url": hook_server})
    Hook.mode = "fail"
    issue = mk(client, title="backoff 게이트")
    d1 = client.post(f"/issues/{issue['id']}/dispatch",
                     json={"agent": "demo", "message": "메시지"}).json()
    assert d1["status"] == "error"
    Hook.received.clear()
    Hook.mode = "ok"
    r = client.post(f"/issues/{issue['id']}/dispatch",
                    json={"agent": "demo", "message": "메시지"})
    assert r.status_code == 200 and r.json()["status"] == "error"  # 기수락
    assert len(Hook.received) == 0  # 웹훅 미호출


def test_backoff_경과후_재전달은_웹훅을_치고_카운터를_초기화한다(client, hook_server):
    client.post("/agents", json={"name": "demo", "base_url": hook_server})
    Hook.mode = "fail"
    issue = mk(client, title="복구")
    d1 = client.post(f"/issues/{issue['id']}/dispatch",
                     json={"agent": "demo", "message": "메시지"}).json()
    assert d1["status"] == "error"
    Hook.mode = "ok"
    _backdate(client, d1["id"])
    d2 = client.post(f"/issues/{issue['id']}/dispatch",
                     json={"agent": "demo", "message": "메시지"}).json()
    assert d2["id"] == d1["id"] and d2["status"] == "ok"
    assert _row(client, d1["id"])["fail_count"] == 0  # 성공 시 초기화
    assert _row(client, d1["id"])["last_fail_at"] is None


def test_5회_연속_실패시_사람_통보가_한_번_나간다(client):
    client.post("/agents", json={"name": "demo", "base_url": DEAD_HOOK})
    issue = mk(client, title="사람 통보")
    d1 = client.post(f"/issues/{issue['id']}/dispatch",
                     json={"agent": "demo", "message": "메시지"}).json()
    assert d1["status"] == "error"
    # fail_count=4로 미리 세팅하고 backoff 경과 후 재시도 → 5회째
    con = sqlite3.connect(client.app.state.ctx.db_path)
    con.execute("UPDATE dispatches SET fail_count=4, last_fail_at='2020-01-01T00:00:00+0900' "
                "WHERE id=?", (d1["id"],))
    con.commit()
    con.close()
    d2 = client.post(f"/issues/{issue['id']}/dispatch",
                     json={"agent": "demo", "message": "메시지"}).json()
    assert d2["status"] == "error" and _row(client, d1["id"])["fail_count"] == 5
    human = [c for c in _fail_comments(client, issue["id"]) if "사람 확인" in c["body"]]
    assert len(human) == 1
    # 6회째 — 추가 통보 없음
    _backdate(client, d1["id"])
    d3 = client.post(f"/issues/{issue['id']}/dispatch",
                     json={"agent": "demo", "message": "메시지"}).json()
    assert d3["status"] == "error" and _row(client, d1["id"])["fail_count"] == 6
    human = [c for c in _fail_comments(client, issue["id"]) if "사람 확인" in c["body"]]
    assert len(human) == 1
    plain = [c for c in _fail_comments(client, issue["id"]) if "사람 확인" not in c["body"]]
    assert len(plain) == 1  # 일반 실패 댓글도 여전히 1건
