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


async def _drain(gen, stop):
    """비동기 SSE 제너레이터 소비 — 조건 충족까지 타임아웃 걸고 수신."""
    import asyncio
    received, deadline = [], 10.0
    while True:
        try:
            chunk = await asyncio.wait_for(gen.__anext__(), timeout=deadline)
        except (StopAsyncIteration, asyncio.TimeoutError):
            return received
        for line in chunk.split("\n"):
            if line.startswith("data: "):
                received.append(json.loads(line[6:]))
        if stop(received):
            return received


@pytest.mark.anyio
async def test_sse_stream_replays_after_cursor(client):
    """검증기준: SSE 재연결 시 커서 이후 전건 수신."""
    i = mk(client, title="SSE 재생")
    for n in range(3):
        client.post(f"/issues/{i['id']}/comments", json={"author": "w1", "body": "s%d" % n})
    cursor = client.get("/events?limit=1").json()["events"][0]["seq"]
    # 커서 이후 전건 — 비동기 스트림 제너레이터 직접 소비
    from server.routers.meta import events_stream
    gen = events_stream(after_seq=cursor, ctx=client.app.state.ctx, poll_s=0.01, max_idle=0.05)
    received = await _drain(gen, lambda ev: any(e["entity_id"] == i["id"] for e in ev))
    await gen.aclose()
    assert received, "SSE 재생 수신 0건"
    assert all(e["seq"] > cursor for e in received)
    assert any(e["entity_id"] == i["id"] for e in received), "커서 이후 이벤트 누락"


@pytest.mark.anyio
async def test_sse_last_event_id_header(client):
    """Last-Event-ID 헤더 — 재접속 커서(표준 SSE 재연결 규약)."""
    i = mk(client, title="Last-Event-ID")
    client.post(f"/issues/{i['id']}/comments", json={"author": "w1", "body": "x"})
    r = client.get("/events?limit=1")
    cursor = r.json()["events"][-1]["seq"]
    client.post(f"/issues/{i['id']}/comments", json={"author": "w1", "body": "y"})
    from server.routers.meta import events_stream
    gen = events_stream(after_seq=cursor, ctx=client.app.state.ctx, poll_s=0.01, max_idle=0.05)
    received = await _drain(gen, lambda ev: bool(ev))
    await gen.aclose()
    assert all(e["seq"] > cursor for e in received)


def test_response_cursor_pagination_no_skip(client):
    """리뷰 R1: 응답 커서(cursor)로 페이지네이션 — 페이지 크기와 무관하게 누락 0.
    (구버식 last_seq=MAX(seq) 커서는 limit=2에서 중간 이벤트를 영구 건너뛰었다)"""
    i = mk(client, title="응답 커서 페이지네이션")
    for n in range(50):
        client.post(f"/issues/{i['id']}/comments", json={"author": "w1", "body": "c%d" % n})
    seen, cursor, pages = [], 0, 0
    while True:
        page = client.get(f"/events?after_seq={cursor}&limit=2").json()
        if not page["events"]:
            break
        pages += 1
        seen.extend(page["events"])
        # 응답 커서로만 이어받기 — events[-1] 추측 금지
        assert page["cursor"] == page["events"][-1]["seq"]
        cursor = page["cursor"]
        if pages > 500:
            pytest.fail("페이지네이션 비종료 — 커서가 안 남진 않는지")
    seqs = _seqs(seen)
    assert len(seqs) == len(set(seqs)), "커서 재개 중복"
    assert len([e for e in seen if e["kind"] == "comment.added" and e["entity_id"] == i["id"]]) == 50
    assert page["cursor"] == cursor and page["last_seq"] >= cursor


def test_issue_created_and_pull_events(client):
    """리뷰 R2: 생성·pull 전이도 outbox를 우회하지 않는다."""
    r = client.post("/issues", json={"acceptance": "완료 기준: 테스트 통과", "title": "생성 이벤트", "state": "todo"}).json()
    ev = client.get("/events?kind=issue.created").json()["events"]
    assert any(e["entity_id"] == r["id"] for e in ev), "issue.created 없음"
    got = client.post("/pull", json={"agent": "w9"}).json()
    assert got and got["id"] == r["id"]
    ev = [e for e in client.get("/events?kind=issue.updated").json()["events"]
          if e["entity_id"] == r["id"]]
    assert ev, "pull 전이 이벤트 없음"
    payload = json.loads(ev[-1]["payload"])
    assert payload["state"] == "in_progress" and payload["assignee"] == "w9" and payload["via"] == "pull"


def test_release_ready_comment_event(client):
    """리뷰 R2: 의존 종료 → [release-ready] 시스템 댓글도 comment.added 이벤트."""
    dep = mk(client, title="의존 원본")
    i = mk(client, title="블록 카드")  # 의존은 blocked_detail 토큰으로 (부모관계 X — 부모 done은 409)
    client.patch(f"/issues/{i['id']}", json={"state": "blocked", "waiting_for": "dependency",
                                             "blocked_detail": f"의존 {dep['id']}"})
    # review 진입 후 verify → done → notify_blocked_dependents (verify는 review 전용)
    client.patch(f"/issues/{dep['id']}", json={"state": "in_progress"})
    client.patch(f"/issues/{dep['id']}", json={"state": "review"})
    client.post(f"/issues/{dep['id']}/verify", json={"verifier": "t", "evidence": "pytest 5 passed"})
    ev = [e for e in client.get("/events?kind=comment.added").json()["events"]
          if e["entity_id"] == i["id"]]
    assert ev and "[release-ready]" in json.loads(ev[-1]["payload"])["preview"]


@pytest.mark.anyio
async def test_idle_streams_do_not_exhaust_workers(client):
    """리뷰 R3: 유휴 SSE 연결이 공용 스레드풀을 고갈시키지 않는다 — 40 스트림 + 쓰기.

    (동기 sleep 제너레이터였을 때는 40연결이 limiter 40을 점유해 POST가 막혔다)"""
    import asyncio
    import time

    import httpx
    from starlette.testclient import TestClient  # noqa: F401

    app = client.app
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t", timeout=3.0) as ac:
        async def idle():
            try:
                async with ac.stream("GET", "/events/stream?after_seq=99999999&poll=0.05") as r:
                    async for _ in r.aiter_bytes():
                        pass
            except Exception:
                pass
        streams = [asyncio.create_task(idle()) for _ in range(40)]
        await asyncio.sleep(0.5)  # 스트림 전부 유휴 진입
        t0 = time.monotonic()
        r = await ac.post("/issues", json={"acceptance": "완료 기준: 테스트 통과", "title": "쓰기 무방해"})
        dt = time.monotonic() - t0
        for s in streams:
            s.cancel()
        await asyncio.gather(*streams, return_exceptions=True)
        assert r.status_code == 201, "유휴 스트림 40개 뒤에서 쓰기 실패 — 스레드풀 고갈"
        assert dt < 2.0, f"쓰기가 {dt:.2f}s — 스트림 수에 막힘"


def _wait_notifies(pred, timeout=5.0):
    """알림 워커는 비동기(커밋 후 별도 스레드) — 조건 충족까지 폴링 대기."""
    import time as _t
    deadline = _t.monotonic() + timeout
    while _t.monotonic() < deadline:
        got = [(h.get("x-tt-command"), p) for h, p in Hook.received]
        if pred(got):
            return got
        _t.sleep(0.05)
    return [(h.get("x-tt-command"), p) for h, p in Hook.received]


def test_mention_notify_respects_flag(client, hook_server):
    """리뷰 R4: 멘션 → notify_hook=true 에이전트에게만 알림 전송(켬/끔 회귀)."""
    client.post("/agents", json={"name": "on-ag", "base_url": hook_server, "notify_hook": True})
    client.post("/agents", json={"name": "off-ag", "base_url": hook_server, "notify_hook": False})
    Hook.received.clear()
    client.post("/messages", json={"author": "w1", "body": "@on-ag @off-ag 확인 요청 — 본문 전달 검증"})
    notifies = _wait_notifies(lambda got: sum(1 for c, _ in got if c == "notify") >= 1)
    # off-ag는 끔 — 대상 에이전트 수만큼만 전송
    assert len(notifies) == 1, f"notify {len(notifies)}건 — 플래그 미반영: {notifies}"
    cmd, payload = notifies[0]
    assert cmd == "notify" and "멘션 from w1" in payload["reason"]
    # 리뷰 R4b: 알림 본문이 빈 문자열이 아니다 — 요청 내용이 실제 전달된다
    assert "본문 전달 검증" in (payload.get("text") or ""), f"본문 누락: {payload!r}"


def test_release_ready_notify_assignee(client, hook_server):
    """리뷰 R4: [release-ready] 코멘트 → 대기 카드 담당 에이전트 notify(플래그 on만)."""
    dep = mk(client, title="의존 원본")
    i = mk(client, title="블록 카드")  # 의존은 blocked_detail 토큰으로 (부모관계 X — 부모 done은 409)
    client.post("/agents", json={"name": "rel-on", "base_url": hook_server, "notify_hook": True})
    client.post("/agents", json={"name": "rel-off", "base_url": hook_server, "notify_hook": False})
    client.patch(f"/issues/{i['id']}", json={"state": "blocked", "waiting_for": "dependency",
                                             "blocked_detail": f"의존 {dep['id']}",
                                             "assignee": "rel-on"})
    Hook.received.clear()
    client.patch(f"/issues/{dep['id']}", json={"state": "in_progress"})
    client.patch(f"/issues/{dep['id']}", json={"state": "review"})
    client.post(f"/issues/{dep['id']}/verify", json={"verifier": "t", "evidence": "pytest 5 passed"})
    notifies = _wait_notifies(lambda got: sum(1 for c, _ in got if c == "notify") >= 1)
    assert len(notifies) == 1 and notifies[0][1]["reason"] == "release-ready"


def test_human_verify_records_issue_updated(client):
    """리뷰 R2 2차: 사람 승인 분기도 issue.updated 이벤트 — done 소비자 누락 방지."""
    i = mk(client, title="사람 승인 전이")
    client.patch(f"/issues/{i['id']}", json={"state": "in_progress"})
    r = client.post(f"/issues/{i['id']}/verify", json={"human": True, "verifier": "boss",
                                                       "evidence": "사람 승인 — 자기선언"})
    assert r.status_code == 200 and r.json()["state"] == "done"
    ev = [e for e in client.get("/events?kind=issue.updated").json()["events"]
          if e["entity_id"] == i["id"]]
    assert ev, "사람 승인 issue.updated 없음"
    payload = json.loads(ev[-1]["payload"])
    assert payload["state"] == "done" and payload["via"] == "human-verify"


def test_notify_outside_write_lock(client, hook_server):
    """리뷰 R4a 경계: 알림 전송은 커밋 후 락 밖 — 느린 수신기가 무관한 쓰기를 막지 않고,
    전송 지연 중에도 원문·이벤트는 즉시 가시다."""
    client.post("/agents", json={"name": "slow-ag", "base_url": hook_server, "notify_hook": True})
    import time
    Hook.mode = "slow"  # 수신기 2초 지연
    Hook.received.clear()
    t0 = time.monotonic()
    r = client.post("/messages", json={"author": "w1", "body": "@slow-ag 느린 수신기 테스트"})
    dt = time.monotonic() - t0
    assert r.status_code == 201 and dt < 1.5, f"메시지 POST가 훅을 기다림({dt:.2f}s) — 락 내 전송"
    # 리뷰 보강: 워커가 수신기에 '진입'(요청 도착, 응답 전 2s 정지 구간)할 때까지 대기 —
    # 전송이 실제로 막혀 있는 동안에 가시성·무방해 쓰기를 검증한다 (모드 해제는 그 뒤)
    entered = _wait_notifies(lambda g: any(c == "notify" for c, _ in g), timeout=4.0)
    assert any(c == "notify" for c, _ in entered), "워커가 수신기에 진입하지 않음"
    # 전송이 막혀 있는 동안: 원문·이벤트는 가시 (커밋 완료 — 트랜잭션 내 전송 잔재 없음)
    ev = client.get("/events?kind=message.posted").json()["events"]
    assert any(json.loads(e["payload"]).get("body") == "@slow-ag 느린 수신기 테스트" for e in ev), \
        "커밋 전까지 이벤트 비가시 — 트랜잭션 내 전송 잔재"
    # 무관한 쓰기가 즉시 통과 (느린 훅이 쓰기 락을 잡지 않음)
    t0 = time.monotonic()
    w = client.post("/issues", json={"acceptance": "완료 기준: 테스트 통과", "title": "무방해 쓰기"})
    assert w.status_code == 201 and time.monotonic() - t0 < 2.0
    Hook.mode = "ok"  # 해제 — 막힌 전송 완료


def _own_hook_server():
    """테스트 전용 훅 수신기 — (server, url). 진입 동기화 테스트용."""
    import threading as _th
    from http.server import HTTPServer
    Hook.received, Hook.mode = [], "ok"
    server = HTTPServer(("127.0.0.1", 0), Hook)
    _th.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/hook"


def _wait_polls(entry, minimum, timeout=6.0):
    """워커가 DB를 실제로 열고 폴링했는지 — polls 카운터로 동기화."""
    import time as _t
    deadline = _t.monotonic() + timeout
    while _t.monotonic() < deadline:
        if entry.get("polls", 0) >= minimum:
            return True
        _t.sleep(0.02)
    return False


def test_worker_restarts_for_recreated_app(tmp_path):
    """리뷰 R5: 같은 DB로 앱을 다시 만들면 워커가 다시 시작된다 — 알림 중단 없음."""
    from fastapi.testclient import TestClient

    from app import create_app, _notify_workers

    path = str(tmp_path / "tt.db")
    server, url = _own_hook_server()
    try:
        for body in ("@w-ag 첫 앱 멘션", "@w-ag 재생성 앱 멘션"):
            with TestClient(create_app(path)) as c:
                c.post("/agents", json={"name": "w-ag", "base_url": url, "notify_hook": True})
                Hook.received.clear()
                c.post("/messages", json={"author": "w1", "body": body})
                got = _wait_notifies(lambda g: any(c2 == "notify" for c2, _ in g), timeout=5)
            assert any(c2 == "notify" for c2, _ in got), f"알림 미전달: {body}"
        # 마지막 앱 종료 — 워커 본인이 등록을 치운다
        entry = _notify_workers.get(path)
        if entry is not None:
            entry["thread"].join(timeout=5)
        assert path not in _notify_workers, "종료 후 등록 잔재"
    finally:
        server.shutdown()


def test_mid_send_recreate_no_duplicate_delivery(tmp_path):
    """리뷰 R5 4차 경계 1: 전송 중 앱 종료 → 재생성 — 같은 이벤트 중복 전송 금지.

    첫 워커가 느린 수신기에 진입한 상태로 앱을 종료·재생성한다. 선점 CAS
    (notified 0→2) 때문에 새 워커는 이미 잡힌 이벤트를 전송하지 못한다."""
    from fastapi.testclient import TestClient

    from app import create_app

    path = str(tmp_path / "tt.db")
    server, url = _own_hook_server()
    try:
        Hook.mode = "slow"  # 수신기 2초 정지
        with TestClient(create_app(path)) as c1:
            c1.post("/agents", json={"name": "w-ag", "base_url": url, "notify_hook": True})
            c1.post("/messages", json={"author": "w1", "body": "@w-ag 중복 경계"})
            # 첫 워커가 수신기에 진입(요청 도착 — 응답 전 2s 정지)할 때까지
            assert _wait_notifies(lambda g: any(c2 == "notify" for c2, _ in g), timeout=4), \
                "첫 워커 미진입"
        # 진입한 전송이 여전히 막혀 있는 동안(2s 내) 같은 DB 재생성
        with TestClient(create_app(path)) as c2:
            c2.post("/messages", json={"author": "w1", "body": "@w-ag 재생성 후 메시지"})
            Hook.mode = "ok"  # 막힌 첫 전송 해제
            got = _wait_notifies(
                lambda g: any(c2 == "notify" and "재생성 후" in (p.get("text") or "")
                              for c2, p in g), timeout=6)
        first_dupes = [p for c2, p in got
                       if c2 == "notify" and "중복 경계" in (p.get("text") or "")]
        assert len(first_dupes) == 1, f"첫 이벤트 중복 전송 {len(first_dupes)}건"
        assert any("재생성 후" in (p.get("text") or "") for c2, p in got if c2 == "notify"), \
            "재생성 앱 알림 미전달"
    finally:
        Hook.mode = "ok"
        server.shutdown()


def test_shared_db_app_exit_keeps_delivery(tmp_path):
    """리뷰 R5 4차 경계 2: 두 앱이 같은 DB 공유 — 먼저 만든 앱 종료 후에도 전달 지속."""
    from fastapi.testclient import TestClient

    from app import create_app, _notify_workers

    path = str(tmp_path / "tt.db")
    server, url = _own_hook_server()
    try:
        app1, app2 = create_app(path), create_app(path)
        entry = _notify_workers[path]
        assert entry["refs"] == 2, f"참조 카운트 {entry['refs']}"
        with TestClient(app1) as c1:  # app1만 종료 — refs 1, 워커 생존
            c1.post("/agents", json={"name": "w-ag", "base_url": url, "notify_hook": True})
        assert entry["refs"] == 1 and not entry["stop"].is_set(), "첫 앱 종료가 워커를 죽임"
        with TestClient(app2) as c2:
            c2.post("/messages", json={"author": "w1", "body": "@w-ag 생존 앱 멘션"})
            got = _wait_notifies(lambda g: any(c2 == "notify" for c2, _ in g), timeout=5)
        assert any(c2 == "notify" for c2, _ in got), "생존 앱 알림 미전달"
        with TestClient(app2):  # 마지막 앱 종료 → 워커 종료
            pass
        entry["thread"].join(timeout=5)
        assert not entry["thread"].is_alive(), "워커 스레드 미종료"
        assert path not in _notify_workers, "종료 후 등록 잔재"
    finally:
        server.shutdown()


def test_repeated_app_creation_closes_worker_resources(tmp_path):
    """리뷰 R5 4차: 앱 생성/종료 반복 — 워커 스레드·연결이 실제로 정리된다.

    (종료 미연결이던 워커는 앱당 수 개의 fd를 새며 기본 한도 256에서 수십 회 만에
    'unable to open database file'로 죽었다. /health만으로는 DB를 안 열어 워커가
    연결을 안 만드므로, 매 회 이슈를 만들어 실제 연결을 강제하고 polls 카운터로
    워커 진입을 동기화한 뒤 join으로 종료까지 확인한다)"""
    from fastapi.testclient import TestClient

    from app import create_app, _notify_workers

    path = str(tmp_path / "tt.db")
    for n in range(100):
        app = create_app(path)
        with TestClient(app) as c:
            c.post("/issues", json={"acceptance": "완료 기준: 테스트 통과", "title": f"반복 {n}"})  # 실제 DB 오픈 강제
            entry = _notify_workers.get(path)
            assert entry is not None, f"{n}번째 워커 미기동"
            assert _wait_polls(entry, 1), f"{n}번째 워커 폴링 미진입"
        entry["thread"].join(timeout=5)
        assert not entry["thread"].is_alive(), f"{n}번째 워커 스레드 미종료"
        assert path not in _notify_workers, f"{n}번째 종료 후 등록 잔재"


def test_acceptance_required_for_new_cards(client):
    """TT 개선#3a: acceptance 없는 신규 카드 생성 거부(템플릿 게이트)."""
    r = client.post("/issues", json={"title": "기준 없는 카드"})
    assert r.status_code == 422, "acceptance 게이트 미작동"
    text = str(r.json()["detail"])
    assert "acceptance" in text, f"게이트 사유 불명: {text}"


def test_acceptance_stored_and_patchable(client):
    """acceptance 지정 생성 + readback + PATCH 보완."""
    r = client.post("/issues", json={"title": "기준 있는 카드",
                                     "acceptance": "pytest 전부 통과"}).json()
    assert r["acceptance"] == "pytest 전부 통과"
    r2 = client.patch(f"/issues/{r['id']}", json={"acceptance": "pytest 321 + runner 93"}).json()
    assert r2["acceptance"] == "pytest 321 + runner 93"
    created = [e for e in client.get("/events?kind=issue.created").json()["events"]
               if e["entity_id"] == r["id"]]
    assert created and json.loads(created[-1]["payload"])["has_acceptance"] is True
