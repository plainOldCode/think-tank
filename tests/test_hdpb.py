"""HDPB(M5283MWX-HDPB) — blocked/gate 카드 필수 필드.

blocked 전이 때 질문·선택지·무응답 기본값을 필수로 받는다(pstack gates.md 형식).
빈 waiting_for는 409. blocked 이탈 시 게이트 필드 소거, blocked 상태에서 부분 갱신 허용.
"""
import json


def _todo(client, **kw):
    kw.setdefault("acceptance", "완료 기준: 테스트 통과")
    return client.post("/issues", json={"title": "HDPB", **kw}).json()


def _gate(**over):
    g = {"waiting_for": "human", "blocked_question": "배포할까요?",
         "blocked_options": ["배포", "보류"], "blocked_default": "보류"}
    g.update(over)
    return g


def test_blocked_전이는_waiting_for가_필수다(client):
    i = _todo(client)
    g = {k: v for k, v in _gate().items() if k != "waiting_for"}
    r = client.patch(f"/issues/{i['id']}", json={"state": "blocked", **g})
    assert r.status_code == 409 and "waiting_for" in r.json()["detail"]


def test_blocked_전이는_질문과_선택지와_기본값이_필수다(client):
    for drop in ("blocked_question", "blocked_options", "blocked_default"):
        i = _todo(client)
        g = {k: v for k, v in _gate().items() if k != drop}
        r = client.patch(f"/issues/{i['id']}", json={"state": "blocked", **g})
        assert r.status_code == 422, (drop, r.text)


def test_빈_질문과_빈_선택지도_거부된다(client):
    i = _todo(client)
    r = client.patch(f"/issues/{i['id']}", json={"state": "blocked", **_gate(blocked_question="   ")})
    assert r.status_code == 422
    j = _todo(client)
    r = client.patch(f"/issues/{j['id']}", json={"state": "blocked", **_gate(blocked_options=["  ", ""])})
    assert r.status_code == 422


def test_기본값은_선택지_중_하나여야_한다(client):
    i = _todo(client)
    r = client.patch(f"/issues/{i['id']}", json={"state": "blocked", **_gate(blocked_default="무관심")})
    assert r.status_code == 422 and "blocked_default" in r.json()["detail"]


def test_게이트_필드가_저장되고_조회된다(client):
    i = _todo(client)
    r = client.patch(f"/issues/{i['id']}", json={"state": "blocked", **_gate()})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["blocked_question"] == "배포할까요?"
    assert json.loads(body["blocked_options"]) == ["배포", "보류"]
    assert body["blocked_default"] == "보류" and body["waiting_for"] == "human"


def test_blocked_이탈시_게이트_필드가_소거된다(client):
    i = _todo(client)
    client.patch(f"/issues/{i['id']}", json={"state": "blocked", **_gate()})
    r = client.patch(f"/issues/{i['id']}", json={"state": "todo"})
    b = r.json()
    assert b["blocked_question"] == "" and b["blocked_options"] == ""
    assert b["blocked_default"] == "" and b["waiting_for"] == ""


def test_blocked_상태에서_게이트_사족을_부분_갱신한다(client):
    i = _todo(client)
    client.patch(f"/issues/{i['id']}", json={"state": "blocked", **_gate()})
    # 기존 선택지에 없는 기본값 — 병합 판정으로 422
    r = client.patch(f"/issues/{i['id']}", json={"blocked_default": "유지"})
    assert r.status_code == 422
    r = client.patch(f"/issues/{i['id']}", json={"blocked_question": "롤백할까요?",
                                                 "blocked_default": "보류"})
    assert r.status_code == 200 and r.json()["blocked_question"] == "롤백할까요?"
    assert json.loads(r.json()["blocked_options"]) == ["배포", "보류"]


def test_waiting_for_enum과_비blocked_설정_검증은_유지된다(client):
    i = _todo(client)
    r = client.patch(f"/issues/{i['id']}", json={"state": "blocked", **_gate(waiting_for="언젠가")})
    assert r.status_code == 422
    r = client.patch(f"/issues/{i['id']}", json={"waiting_for": "human"})
    assert r.status_code == 409


def test_blocked에서_in_progress로_이탈해도_게이트_필드가_소거된다(client):
    """cursor-review 지적 — blocked→in_progress 재개 시 게이트 사족 잔존 버그.
    blocked 이탈은 목적지 무관하게 소거돼야 한다(문서·criteria 기준)."""
    i = _todo(client)
    client.post(f"/issues/{i['id']}/claim", json={"agent": "w"})
    client.patch(f"/issues/{i['id']}", json={"state": "blocked", **_gate()})
    r = client.patch(f"/issues/{i['id']}", json={"state": "in_progress"})
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["state"] == "in_progress"
    assert b["waiting_for"] == "" and b["blocked_question"] == ""
    assert b["blocked_options"] == "" and b["blocked_default"] == ""


def test_blocked에서_cancelled로_이탈해도_게이트_필드가_소거된다(client):
    i = _todo(client)
    client.post(f"/issues/{i['id']}/claim", json={"agent": "w"})
    client.patch(f"/issues/{i['id']}", json={"state": "blocked", **_gate()})
    r = client.patch(f"/issues/{i['id']}", json={"state": "todo"})
    assert r.status_code == 200
    r = client.patch(f"/issues/{i['id']}", json={"state": "cancelled"})
    b = r.json()
    assert b["waiting_for"] == "" and b["blocked_question"] == ""
    assert b["blocked_options"] == "" and b["blocked_default"] == ""
