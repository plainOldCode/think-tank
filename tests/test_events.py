"""TT 개선#2 (M4DEERNA-CAVF): SQLite 이벤트 로그 + seq 커서 + SSE 알림 경로.

- events 테이블(단조 seq): 이슈·코멘트·메시지·디스패치 변경과 같은 트랜잭션 기록(outbox)
- GET /events?after_seq=N — 유실 없이 이어받기(재접속 커서)
- GET /events/stream?after_seq=N — SSE 단일 엔드포인트(느린 소비자·주기 reconcile)
"""
import json
import sqlite3

import pytest

from conftest import mk, Hook  # noqa: F401


def _seqs(events):
    return [e["seq"] for e in events]


def test_events_table_exists_and_monotonic(client):
    """events 테이블 존재 + seq 단조 증가."""
    con = client.app.state.ctx.con()  # 앱 경유 — SCHEMA 실행 보장
    con.row_factory = sqlite3.Row
    cols = {r["name"] for r in con.execute("PRAGMA table_info(events)").fetchall()}
    con.close()
    assert {"seq", "kind", "entity", "entity_id", "payload", "ts"} <= cols


def test_issue_update_writes_event_same_txn(client):
    """이슈 변경 → issue.updated 이벤트(같은 트랜잭션)."""
    i = mk(client, title="이벤트 이슈")
    client.patch(f"/issues/{i['id']}", json={"state": "in_progress", "assignee": "w1"})
    r = client.get("/events")
    assert r.status_code == 200
    events = [e for e in r.json()["events"] if e["entity"] == "issue" and e["entity_id"] == i["id"]]
    assert events, "issue.updated 이벤트 없음"
    ev = events[-1]
    assert ev["kind"] == "issue.updated"
    payload = json.loads(ev["payload"])
    assert payload["state"] == "in_progress"


def test_comment_writes_event(client):
    """코멘트 접수 → comment.added 이벤트."""
    i = mk(client, title="코멘트 이벤트")
    client.post(f"/issues/{i['id']}/comments", json={"author": "w1", "body": "작업 로그"})
    r = client.get("/events")
    ev = [e for e in r.json()["events"] if e["kind"] == "comment.added" and e["entity_id"] == i["id"]]
    assert ev, "comment.added 이벤트 없음"


def test_message_post_writes_event_with_mentions(client):
    """보드 메시지 → message.posted 이벤트(mentions 포함 — 알림 대상 재사용)."""
    client.post("/agents", json={"name": "w2", "base_url": "http://x/hook"})  # 멘션은 등록 에이전트 대상
    client.post("/messages", json={"author": "w1", "body": "@w2 확인 요청"})
    r = client.get("/events")
    ev = [e for e in r.json()["events"] if e["kind"] == "message.posted"]
    assert ev, "message.posted 이벤트 없음"
    payload = json.loads(ev[-1]["payload"])
    assert payload["mentions"] == ["w2"]


def test_dispatch_events_created_and_terminal(client, hook_server):
    """dispatch 생성·종료 투영 → dispatch 이벤트."""
    i = mk(client, title="디스패치 이벤트")
    client.post("/agents", json={"name": "demo", "base_url": hook_server})
    d = client.post(f"/issues/{i['id']}/dispatch", json={"agent": "demo", "message": "m"}).json()
    r = client.get("/events")
    kinds = [(e["kind"], e["entity_id"]) for e in r.json()["events"]]
    assert ("dispatch.created", str(d["id"])) in kinds
    client.post(f"/issues/{i['id']}/dispatches/{d['id']}/progress",
                json={"state": "finished", "exit": 0, "session": "s1"})
    kinds = [(e["kind"], e["entity_id"]) for e in client.get("/events").json()["events"]]
    assert ("dispatch.updated", str(d["id"])) in kinds


def test_after_seq_cursor_no_loss(client):
    """검증기준: 500건 밀어넣기 후 after_seq 조회로 누락 0건."""
    i = mk(client, title="커서 누락 검증")
    for n in range(500):
        client.post(f"/issues/{i['id']}/comments", json={"author": "w1", "body": "n%d" % n})
    r1 = client.get("/events?limit=1")
    body = r1.json()
    assert body["events"], "첫 조회가 비어 있음"
    first_seq = body["events"][0]["seq"] - 1  # 커서 이전(포함) 이벤트도 회수되는지
    # 커서 이후 전건 수집 — 페이지네이션
    seen, cursor = [], first_seq
    while True:
        page = client.get(f"/events?after_seq={cursor}&limit=200").json()
        if not page["events"]:
            break
        seen.extend(page["events"])
        cursor = page["events"][-1]["seq"]
    seqs = _seqs(seen)
    assert len(seqs) == len(set(seqs)), "중복 이벤트"
    # 500 코멘트 이벤트가 모두 포함(연속 구간 — 유실 0)
    comment_seqs = [e["seq"] for e in seen if e["kind"] == "comment.added" and e["entity_id"] == i["id"]]
    assert len(comment_seqs) == 500
    assert seqs == sorted(seqs), "seq 역전"


def test_sse_stream_replays_after_cursor(client):
    """검증기준: SSE 재연결 시 커서 이후 전건 수신."""
    i = mk(client, title="SSE 재생")
    for n in range(3):
        client.post(f"/issues/{i['id']}/comments", json={"author": "w1", "body": "s%d" % n})
    cursor = client.get("/events?limit=1").json()["events"][0]["seq"]
    # 커서 이후 전건 — 스트림 제너레이터 직접 소비(타임아웃 없이 닫힘까지)
    from server.routers.meta import events_stream
    gen = events_stream(after_seq=cursor, ctx=client.app.state.ctx, poll_s=0.01, max_idle=0.05)
    received = []
    for chunk in gen:
        for line in chunk.split("\n"):
            if line.startswith("data: "):
                received.append(json.loads(line[6:]))
        if any(e["entity_id"] == i["id"] for e in received):
            break
    assert received, "SSE 재생 수신 0건"
    assert all(e["seq"] > cursor for e in received)
    assert any(e["entity_id"] == i["id"] for e in received), "커서 이후 이벤트 누락"


def test_sse_last_event_id_header(client):
    """Last-Event-ID 헤더 — 재접속 커서(표준 SSE 재연결 규약)."""
    i = mk(client, title="Last-Event-ID")
    client.post(f"/issues/{i['id']}/comments", json={"author": "w1", "body": "x"})
    r = client.get("/events?limit=1")
    cursor = r.json()["events"][-1]["seq"]
    client.post(f"/issues/{i['id']}/comments", json={"author": "w1", "body": "y"})
    from server.routers.meta import events_stream
    gen = events_stream(after_seq=cursor, ctx=client.app.state.ctx, poll_s=0.01, max_idle=0.05)
    received = []
    for chunk in gen:
        for line in chunk.split("\n"):
            if line.startswith("data: "):
                received.append(json.loads(line[6:]))
        if received:
            break
    assert all(e["seq"] > cursor for e in received)
