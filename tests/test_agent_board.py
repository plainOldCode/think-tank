"""에이전트 간 메시지 보드 (M4580A48-573W 방향 전환): TT 안의 새 페이지 /agent-board.

- messages 테이블: 스레드 루트(thread_id NULL) + 답글(thread_id=루트 id), author, body,
  mentions(등록 에이전트 @토큰만 서버에서 추출, 콤마 패딩 저장), created_at
- message_reads: (message_id, agent) 읽음 — 멱등
- POST /messages, GET /messages(?thread=&mentions=&since=&limit=), POST /messages/{id}/read,
  GET /messages/unread?agent=
- GET /agent-board — 새 게시판 페이지(StaticFiles 마운트 전 등록, 자산 ?v= 버스팅)
- 일지 철수: GET /issues의 with_comments·lease_by·active_lease 파라미터 제거(하위호환 유지),
  probe journal_tick 제거
"""
import json
import sys

import pytest
from fastapi.testclient import TestClient

from app import create_app


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "p.db"))
    with TestClient(app) as c:
        yield c


def _mk_agent(c, name):
    assert c.post("/agents", json={"name": name, "base_url": "http://x"}).status_code in (200, 201)


def test_메시지_작성_기본(client):
    r = client.post("/messages", json={"author": "codex", "body": "배포 완료 보고"})
    assert r.status_code == 201, r.text
    m = r.json()
    assert m["author"] == "codex" and m["body"] == "배포 완료 보고"
    assert m["thread_id"] is None and m["mentions"] == ""
    assert m["id"] and m["created_at"]


def test_멘션은_등록_에이전트만_추출(client):
    _mk_agent(client, "codex")
    _mk_agent(client, "agy")
    m = client.post("/messages", json={"author": "tp-13",
                                       "body": "@codex @ghost 확인 부탁합니다. @agy도 참고"}).json()
    assert m["mentions"] == ",codex,agy,"


def test_답글_스레드_구성(client):
    root = client.post("/messages", json={"author": "codex", "body": "스레드 질문"}).json()
    r1 = client.post("/messages", json={"author": "agy", "body": "답변 1",
                                        "thread_id": root["id"]})
    assert r1.status_code == 201
    assert r1.json()["thread_id"] == root["id"]
    # 답글에 대한 답글도 루트 스레드로 평탄화
    r2 = client.post("/messages", json={"author": "codex", "body": "재질문",
                                        "thread_id": r1.json()["id"]}).json()
    assert r2["thread_id"] == root["id"]
    # 존재하지 않는 스레드
    assert client.post("/messages", json={"author": "a", "body": "b",
                                          "thread_id": "MXXX-XXXX"}).status_code == 422
    th = client.get(f"/messages?thread={root['id']}").json()
    assert [m["id"] for m in th] == [root["id"], r1.json()["id"], r2["id"]], "루트+답글 시간순"


def test_멘션_필터(client):
    _mk_agent(client, "codex")
    client.post("/messages", json={"author": "tp-13", "body": "@codex 지시사항"})
    client.post("/messages", json={"author": "tp-13", "body": "전체 공지"})
    ms = client.get("/messages?mentions=codex").json()
    assert len(ms) == 1 and "codex" in ms[0]["mentions"]


def test_읽음_표시와_unread(client):
    _mk_agent(client, "codex")
    _mk_agent(client, "agy")
    m1 = client.post("/messages", json={"author": "agy", "body": "@codex 봐줘"}).json()
    m2 = client.post("/messages", json={"author": "codex", "body": "내가 쓴 것"}).json()
    assert client.get("/messages/unread?agent=codex").json()["count"] == 1
    assert client.post(f"/messages/{m1['id']}/read", json={"agent": "codex"}).status_code == 200
    # 멱등
    assert client.post(f"/messages/{m1['id']}/read", json={"agent": "codex"}).status_code == 200
    assert client.get("/messages/unread?agent=codex").json()["count"] == 0
    assert client.get("/messages/unread?agent=agy").json()["count"] == 1  # codex의 m2
    assert client.post("/messages/MXXX-XXXX/read", json={"agent": "codex"}).status_code == 404
    # 읽음 목록이 조회에 노출
    ms = {m["id"]: m for m in client.get("/messages").json()}
    assert "codex" in ms[m1["id"]]["reads"]


def test_작성_검증(client):
    assert client.post("/messages", json={"author": "", "body": "x"}).status_code == 422
    assert client.post("/messages", json={"author": "codex", "body": ""}).status_code == 422
    assert client.post("/messages", json={"author": "codex"}).status_code == 422


def test_limit과_기본_정렬(client):
    for n in range(5):
        client.post("/messages", json={"author": f"a{n}", "body": f"m{n}"})
    ms = client.get("/messages?limit=3").json()
    assert len(ms) == 3
    assert ms[0]["body"] == "m4", "최신 먼저"


def test_agent_board_페이지(client):
    r = client.get("/agent-board")
    assert r.status_code == 200
    html = r.text
    assert "agent-board" in html and "?v=" in html, "자산 버스팅 적용"
    assert "Cache-Control" in r.headers


def test_issues_리스트에서_일지용_파라미터_철수(client):
    iid = client.post("/issues", json={"acceptance": "완료 기준: 테스트 통과", "title": "x"}).json()["id"]
    client.post(f"/issues/{iid}/comments", json={"author": "a", "body": "b"})
    row = next(r for r in client.get("/issues?with_comments=1").json() if r["id"] == iid)
    assert "comments" not in row, "일지 철수 — with_comments 제거(알 수 없는 파라미터 무시)"
    # 철수된 파라미터는 필터로 동작하지 않는다(알 수 없는 쿼리 파라미터 무시)
    assert any(r["id"] == iid for r in client.get("/issues?lease_by=없음&active_lease=1").json())


def test_probe에서_journal_철수():
    import os
    assert not os.path.exists(os.path.join(os.path.dirname(__file__), "..", "server", "probe", "journal.py"))
    src = open(os.path.join(os.path.dirname(__file__), "..", "server", "probe", "core.py")).read()
    assert "journal_tick" not in src
    assert '"일지" not in' not in src, "decide.idle의 일지 제외 조건도 제거"


def test_mentions_와일드카드_이름_정확매칭(client):
    """codex F4: LIKE 와일드카드(_)를 문자 그대로 — foo_bar 조회에 fooXbar가 묻지 않는다."""
    _mk_agent(client, "foo_bar")
    _mk_agent(client, "fooXbar")
    client.post("/messages", json={"author": "tp-13", "body": "@fooXbar 보세요"})
    assert client.get("/messages?mentions=foo_bar").json() == []
    assert len(client.get("/messages?mentions=fooXbar").json()) == 1


def test_since_같은초_경계_포함(client):
    """codex F5: 초 단위 커서의 배타적 비교는 같은 초 메시지를 놓친다 — 경계 포함으로."""
    m1 = client.post("/messages", json={"author": "a", "body": "m1"}).json()
    m2 = client.post("/messages", json={"author": "b", "body": "m2"}).json()
    ms = client.get(f"/messages?since={m1['created_at']}").json()
    assert m2["id"] in {m["id"] for m in ms}, "같은 초 작성분 누락 금지(경계 재수신은 클라이언트가 id로 중복 제거)"
