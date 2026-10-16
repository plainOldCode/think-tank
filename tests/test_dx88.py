"""DX88(M5283J3A-DX88) — 수령자 이름 정규화: claim·pull·claim-review·PATCH assignee에서
Z1HP 별칭(agents.aliases)→등록명 치환, 빈 값·숫자만 있는 값 거부,
TT_REQUIRE_KNOWN_AGENT=1 게이트 시 미등록 이름 거부(policy: assignee_unregistered).

배경: assignee 고유값 20+ 대 등록 에이전트 10개 — codex@m2pro 같은 미등록 표기가 저장되면
probe 리뷰 dispatch에서 404로 낙하한다(에픽 #1 직접 원인).
"""
import pytest


def _agent(client, name, aliases=None, **kw):
    payload = {"name": name, "base_url": "http://x/hook", "receive_mode": "hook", **kw}
    if aliases is not None:
        payload["aliases"] = aliases
    return client.post("/agents", json=payload)


def _todo(client, **kw):
    kw.setdefault("acceptance", "완료 기준: 테스트 통과")
    return client.post("/issues", json={"title": "DX88", **kw}).json()


# --- 별칭 정규화 ---

def test_별칭으로_claim하면_등록명이_저장된다(client):
    _agent(client, "codex", aliases=["codex@m2pro"])
    i = _todo(client)
    r = client.post(f"/issues/{i['id']}/claim", json={"agent": "codex@m2pro"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["assignee"] == "codex" and body["lease_by"] == "codex"


def test_등록명_정확_일치가_별칭보다_우선한다(client):
    _agent(client, "codex", aliases=["alias-x"])
    i = _todo(client)
    r = client.post(f"/issues/{i['id']}/claim", json={"agent": "codex"})
    assert r.status_code == 200 and r.json()["assignee"] == "codex"


def test_공백이_붙은_이름은_정리되어_저장된다(client):
    _agent(client, "codex")
    i = _todo(client)
    r = client.post(f"/issues/{i['id']}/claim", json={"agent": "  codex  "})
    assert r.status_code == 200 and r.json()["assignee"] == "codex"


def test_pull에서도_별칭이_정규화된다(client):
    _agent(client, "codex", aliases=["codex@m2pro"])
    i = _todo(client, priority=1)
    r = client.post("/pull", json={"agent": "codex@m2pro"})
    assert r.status_code == 200, r.text
    assert r.json()["id"] == i["id"] and r.json()["assignee"] == "codex"


def test_claim_review에서도_별칭이_정규화된다(client):
    _agent(client, "worker")
    _agent(client, "rev", aliases=["rev@mini"])
    i = _todo(client)
    client.post(f"/issues/{i['id']}/claim", json={"agent": "worker"})
    client.patch(f"/issues/{i['id']}", json={"state": "review"})
    r = client.post(f"/issues/{i['id']}/claim-review", json={"agent": "rev@mini"})
    assert r.status_code == 200, r.text
    assert r.json()["reviewer"] == "rev"


def test_patch_assignee도_별칭을_정규화한다(client):
    _agent(client, "codex", aliases=["codex@m2pro"])
    i = _todo(client)
    r = client.patch(f"/issues/{i['id']}", json={"assignee": "codex@m2pro"})
    assert r.status_code == 200 and r.json()["assignee"] == "codex"


# --- 정크 거부 ---

def test_빈_에이전트_이름_claim은_거부된다(client):
    i = _todo(client)
    assert client.post(f"/issues/{i['id']}/claim", json={"agent": ""}).status_code == 422
    assert client.post(f"/issues/{i['id']}/claim", json={"agent": "   "}).status_code == 422


def test_숫자만_있는_에이전트_이름은_거부된다(client):
    i = _todo(client)
    assert client.post(f"/issues/{i['id']}/claim", json={"agent": "1"}).status_code == 422
    assert client.post(f"/issues/{i['id']}/claim", json={"agent": "12345"}).status_code == 422


def test_정크_이름_pull과_patch도_거부된다(client):
    assert client.post("/pull", json={"agent": "1"}).status_code == 422
    i = _todo(client)
    assert client.patch(f"/issues/{i['id']}", json={"assignee": "1"}).status_code == 422


def test_patch_assignee_빈_값은_해제로_허용된다(client):
    _agent(client, "codex")
    i = _todo(client)
    client.post(f"/issues/{i['id']}/claim", json={"agent": "codex"})
    r = client.patch(f"/issues/{i['id']}", json={"assignee": ""})
    assert r.status_code == 200 and r.json()["assignee"] == "" and r.json()["lease_by"] == ""


# --- 게이트(TT_REQUIRE_KNOWN_AGENT) ---

def test_게이트_활성화_시_미등록_이름_claim은_거부된다(client, monkeypatch):
    monkeypatch.setenv("TT_REQUIRE_KNOWN_AGENT", "1")
    i = _todo(client)
    r = client.post(f"/issues/{i['id']}/claim", json={"agent": "ghost@nowhere"})
    assert r.status_code == 409 and "assignee_unregistered" in r.json()["detail"]
    # 등록 후에는 통과
    _agent(client, "ghost", aliases=["ghost@nowhere"])
    assert client.post(f"/issues/{i['id']}/claim", json={"agent": "ghost@nowhere"}).status_code == 200


def test_게이트_활성화_시_미등록_이름_pull도_거부된다(client, monkeypatch):
    monkeypatch.setenv("TT_REQUIRE_KNOWN_AGENT", "1")
    _todo(client, priority=1)
    r = client.post("/pull", json={"agent": "ghost@nowhere"})
    assert r.status_code == 409 and "assignee_unregistered" in r.json()["detail"]


def test_게이트_활성화_시_patch_assignee_미등록도_거부된다(client, monkeypatch):
    monkeypatch.setenv("TT_REQUIRE_KNOWN_AGENT", "1")
    i = _todo(client)
    r = client.patch(f"/issues/{i['id']}", json={"assignee": "ghost@nowhere"})
    assert r.status_code == 409 and "assignee_unregistered" in r.json()["detail"]


def test_게이트_기본값은_비활성으로_미등록_이름도_수령된다(client):
    """하위 호환 — 게이트 off에서는 현행 동작 유지(등록 유도는 409 메시지로 대체 불가)."""
    i = _todo(client)
    r = client.post(f"/issues/{i['id']}/claim", json={"agent": "skshim@tp-13"})
    assert r.status_code == 200 and r.json()["assignee"] == "skshim@tp-13"


# --- 기존 동작 회귀 ---

def test_재claim_충돌은_정규화된_이름으로_판정한다(client):
    """todo 상태에 assignee 선행 지정 카드 — 별칭 claim은 본인으로 인정(200),
    타 에이전트는 already claimed 409. 정규화 전이면 별칭 claim이 우회 통과한다."""
    _agent(client, "codex", aliases=["codex@m2pro"])
    _agent(client, "other")
    j = _todo(client)
    client.patch(f"/issues/{j['id']}", json={"assignee": "codex"})
    assert client.post(f"/issues/{j['id']}/claim", json={"agent": "codex@m2pro"}).status_code == 200
    k = _todo(client)
    client.patch(f"/issues/{k['id']}", json={"assignee": "codex"})
    r = client.post(f"/issues/{k['id']}/claim", json={"agent": "other"})
    assert r.status_code == 409 and "already claimed by codex" in r.json()["detail"]


def test_lease_한도_판정도_정규화된_이름으로_집계한다(client):
    _agent(client, "w", aliases=["w@t"])
    a = _todo(client)
    b = _todo(client, priority=2)
    assert client.post(f"/issues/{a['id']}/claim", json={"agent": "w"}).status_code == 200
    # TT_MAX_LEASES 기본 2 — 같은 에이전트(별칭 표기) 두 번째 lease는 성공
    assert client.post(f"/issues/{b['id']}/claim", json={"agent": "w@t"}).status_code == 200
    c = _todo(client, priority=3)
    r = client.post(f"/issues/{c['id']}/claim", json={"agent": "w"})
    assert r.status_code == 409 and "lease limit" in r.json()["detail"]
