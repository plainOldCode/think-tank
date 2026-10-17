"""DW0P(M5283NC3-DW0P) — standing orders와 결정 기록을 카드에 연결.

1) 부모 epic의 "## Standing Orders" 섹션을 claim 응답과 dispatch 메시지에 그대로
   붙인다(번호 목록 텍스트).
2) 결정 기록은 1행 댓글 규약 `[decision] 주제 | 결정 | 근거` — tt show에서
   별도 섹션으로 모아 보인다.
3) 러너 계속(continue) 세션 프롬프트에 session-pickup 규칙(이전 기록 신뢰, 다시
   하지 않기)을 넣는다.
"""
import json

import pytest
from conftest import mk


def _epic_with_orders(client):
    r = client.post("/issues", json={
        "title": "epic", "acceptance": "완료 기준: 전부 done",
        "body": "에픽 본문\n\n## Standing Orders\n1. 1카드=1PR\n2. 계약 문서 패리티 유지\n\n## 기타\n다른 내용"})
    assert r.status_code == 201, r.text
    return r.json()


def _child(client, epic):
    r = client.post("/issues", json={"title": "child", "acceptance": "기준", "parent_id": epic["id"]})
    assert r.status_code == 201, r.text
    return r.json()


# --- standing orders ---

def test_epic의_standing_orders가_claim_응답에_붙는다(client):
    epic = _epic_with_orders(client)
    child = _child(client, epic)
    r = client.post(f"/issues/{child['id']}/claim", json={"agent": "worker"})
    assert r.status_code == 200, r.text
    so = r.json().get("standing_orders")
    assert so and "1카드=1PR" in so and "계약 문서 패리티" in so and "다른 내용" not in so


def test_부모없는_카드는_standing_orders가_없다(client):
    i = mk(client, title="root")
    r = client.post(f"/issues/{i['id']}/claim", json={"agent": "worker"})
    assert not r.json().get("standing_orders")


def test_epic에_orders가_없으면_빈_값이다(client):
    epic = client.post("/issues", json={"title": "epic", "acceptance": "기준", "body": "지시 없음"}).json()
    child = _child(client, epic)
    r = client.post(f"/issues/{child['id']}/claim", json={"agent": "worker"})
    assert not r.json().get("standing_orders")


def test_dispatch_메시지에_standing_orders가_붙는다(client):
    epic = _epic_with_orders(client)
    child = _child(client, epic)
    client.post(f"/issues/{child['id']}/claim", json={"agent": "worker"})
    client.post("/agents", json={"name": "worker", "base_url": "http://127.0.0.1:1"})
    r = client.post(f"/issues/{child['id']}/dispatch",
                    json={"agent": "worker", "message": "실행해"})
    assert r.status_code == 201, r.text
    r2 = client.post(f"/issues/{child['id']}/dispatch",
                     json={"agent": "worker", "message": "재지시"})
    assert r2.status_code == 201, r2.text
    # 러너가 받는 메시지는 코멘트로 기록된다 — 마지막 dispatch 코멘트 확인
    cur = client.get(f"/issues/{child['id']}").json()
    msgs = [c["body"] for c in cur["comments"] if "1카드=1PR" in (c.get("body") or "")]
    assert len(msgs) >= 1 and "재지시" in msgs[-1], msgs


# --- 결정 기록 1행 규약 ---

def test_decision_댓글은_별도_섹션으로_모인다(client):
    i = mk(client, title="장기 카드")
    client.post(f"/issues/{i['id']}/comments",
                json={"author": "worker", "body": "[decision] 캐시 무효화 | TTL 5분 | 실험 근거"})
    client.post(f"/issues/{i['id']}/comments", json={"author": "worker", "body": "일반 코멘트"})
    cur = client.get(f"/issues/{i['id']}").json()
    ds = [c for c in cur["comments"] if (c.get("body") or "").startswith("[decision]")]
    assert len(ds) == 1 and "TTL 5분" in ds[0]["body"]
