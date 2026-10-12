"""Z1HP(M4CKEJGX-Z1HP) — 멘션 별칭 사전: agents.aliases 필드 + 멘션 추출 사전 매핑.

발령자 제안 (a): /agents에 aliases 필드 추가, 멘션 추출 시 별칭→등록명 사전 매핑.
오타 유사도 자동 매칭(c)은 오탐 위험으로 이번 범위에서 제외 — 명시적 사전만.
"""
import json

import pytest


def _agent(client, name, aliases=None, **kw):
    payload = {"name": name, "base_url": "https://agent.example/hook", **kw}
    if aliases is not None:
        payload["aliases"] = aliases
    return client.post("/agents", json=payload)


def _todo(client):
    return client.post("/issues", json={"title": "Z1HP 멘션 별칭", "state": "todo"}).json()


# --- aliases 필드 등록/조회 ---

def test_에이전트에_별칭을_등록하고_조회한다(client):
    r = _agent(client, "ghost-a", aliases=["ga", "감독-A"])
    assert r.status_code == 201
    assert r.json()["aliases"] == ["ga", "감독-A"]
    listed = client.get("/agents").json()
    assert [a["aliases"] for a in listed if a["name"] == "ghost-a"] == [["ga", "감독-A"]]


def test_별칭_미선언_에이전트는_빈_목록을_돌린다(client):
    r = _agent(client, "ghost-b")
    assert r.status_code == 201 and r.json()["aliases"] == []


def test_빈_문자열과_공백_별칭은_거부한다(client):
    assert _agent(client, "ghost-c", aliases=[""]).status_code == 422
    assert _agent(client, "ghost-d", aliases=["   "]).status_code == 422


def test_별칭에_공백과_골뱅이와_쉼표는_거부한다(client):
    assert _agent(client, "ghost-e", aliases=["has space"]).status_code == 422
    assert _agent(client, "ghost-f", aliases=["@self"]).status_code == 422
    assert _agent(client, "ghost-g", aliases=["a,b"]).status_code == 422


def test_다른_에이전트_이름과_겹치는_별칭은_거부한다(client):
    _agent(client, "ghost-h")
    r = _agent(client, "ghost-i", aliases=["ghost-h"])
    assert r.status_code == 422
    assert "ghost-h" in r.json()["detail"]


def test_다른_에이전트별칭과_겹치는_별칭은_거부한다(client):
    _agent(client, "ghost-j", aliases=["shared-alias"])
    r = _agent(client, "ghost-k", aliases=["shared-alias"])
    assert r.status_code == 422


def test_자기_이름과_같은_별칭은_중복이라_거부한다(client):
    r = _agent(client, "ghost-l", aliases=["ghost-l"])
    assert r.status_code == 422


def test_같은_등록에서_중복_별칭은_거부한다(client):
    r = _agent(client, "ghost-m", aliases=["dup", "dup"])
    assert r.status_code == 422


# --- PATCH ---

def test_patch별칭_전면_교체와_빈목록_삭제(client):
    _agent(client, "ghost-n", aliases=["old"])
    r = client.patch("/agents/ghost-n", json={"aliases": ["new1", "new2"]})
    assert r.status_code == 200 and r.json()["aliases"] == ["new1", "new2"]
    r = client.patch("/agents/ghost-n", json={"aliases": []})
    assert r.status_code == 200 and r.json()["aliases"] == []
    # PATCH 생략(None)은 기존 값 유지
    client.patch("/agents/ghost-n", json={"aliases": ["keep"]})
    r = client.patch("/agents/ghost-n", json={"base_url": "https://agent.example/hook2"})
    assert r.status_code == 200 and r.json()["aliases"] == ["keep"]
    # 교체 시 다른 에이전트 이름과 충돌하면 거부
    _agent(client, "ghost-o")
    r = client.patch("/agents/ghost-n", json={"aliases": ["ghost-o"]})
    assert r.status_code == 422


# --- 멘션 추출 사전 매핑 ---

def _post(client, body):
    return client.post("/messages", json={"author": "tester", "body": body}).json()


def test_별칭_멘션이_등록명으로_기록된다(client):
    _agent(client, "ghost-p", aliases=["gp", "지비"])
    m = _post(client, "@gp 작업 부탁한다")
    assert m["mentions"] == ",ghost-p,"
    m = _post(client, "@지비 확인 요청")
    assert m["mentions"] == ",ghost-p,"


def test_별칭도_조사_결합을_허용한다(client):
    _agent(client, "ghost-q", aliases=["gq"])
    m = _post(client, "@gq님 검토 바란다")
    assert m["mentions"] == ",ghost-q,"
    m = _post(client, "@gq-extra는 멘션이 아니다")
    assert m["mentions"] == ""


def test_긴_토큰_우선_매칭은_이름과_별칭_전체에서_한다(client):
    _agent(client, "ghost-r", aliases=["gr-alias"])
    _agent(client, "ghost-s")
    m = _post(client, "@ghost-s와 @gr-alias 둘 다")
    assert m["mentions"] == ",ghost-s,ghost-r,"


def test_미등록_토큰은_여전히_무시된다(client):
    _agent(client, "ghost-t", aliases=["gt"])
    m = _post(client, "@없는놈 @ghost-tu 오타")
    assert m["mentions"] == ""


def test_이름_멘션은_기존과_동일하게_동작한다(client):
    _agent(client, "ghost-u", aliases=["gu"])
    m = _post(client, "@ghost-u 본명도 된다")
    assert m["mentions"] == ",ghost-u,"


# --- 리뷰 P2 회귀 (PR#100 codex 판정) ---

def test_새_등록_이름이_기존_별칭과_같으면_거부한다(client):
    _agent(client, "ghost-v", aliases=["vx"])
    r = _agent(client, "vx")
    assert r.status_code == 422
    assert "ghost-v" in r.json()["detail"]


def test_따옴표_별칭도_충돌을_검출한다(client):
    _agent(client, "ghost-w", aliases=['w"q'])
    r = _agent(client, "ghost-x", aliases=['w"q'])
    assert r.status_code == 422
    r = _agent(client, "ghost-y")
    r = client.patch("/agents/ghost-y", json={"aliases": ['w"q']})
    assert r.status_code == 422


def test_역슬래시_별칭도_충돌을_검출한다(client):
    _agent(client, "ghost-z", aliases=["w\\q"])
    r = _agent(client, "ghost-w2", aliases=["w\\q"])
    assert r.status_code == 422
