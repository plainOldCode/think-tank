"""TT 개선#3d — 자기발의(self-nomination) 규약.

label=self 카드는 backlog에서 시작하고, 에이전트당 열린 self 카드 WIP 1.
todo 승격은 사람/트리아지만 — PATCH에 promoted=true가 있어야 한다.
"""
import pytest


def _create(client, **kw):
    base = {"title": "자기발의 카드", "acceptance": "완료 기준: 규약 통과"}
    base.update(kw)
    return client.post("/issues", json=base)


def test_self_카드는_backlog에서_시작하고_발의자가_배정된다(client):
    r = _create(client, labels=["self"], nominee="a@t", state="todo")
    assert r.status_code == 201
    assert r.json()["state"] == "backlog", "자기발의는 todo 직행 불가"
    assert r.json()["assignee"] == "a@t"


def test_self_카드는_발의자_없으면_거부(client):
    r = _create(client, labels=["self"])
    assert r.status_code == 422
    assert "nominee" in str(r.json()["detail"])


def test_열린_self_카드_둘째는_거부되고_닫으면_다시_가능(client):
    r1 = _create(client, labels=["self"], nominee="a@t")
    assert r1.status_code == 201
    r2 = _create(client, labels=["self"], nominee="a@t", title="두 번째")
    assert r2.status_code == 409, "WIP 1 — 두 번째 자기발의는 거부"
    assert "WIP 1" in str(r2.json()["detail"])
    # 다른 에이전트는 자유
    r3 = _create(client, labels=["self"], nominee="b@t", title="남의 것")
    assert r3.status_code == 201
    # 첫 카드 종료 → 다시 발의 가능
    ver = client.get(f"/issues/{r1.json()['id']}").json()["version"]
    client.patch(f"/issues/{r1.json()['id']}", json={"state": "cancelled", "version": ver})
    r4 = _create(client, labels=["self"], nominee="a@t", title="재발의")
    assert r4.status_code == 201


def test_self_카드_todo_승격은_promoted_플래그가_필요하다(client):
    i = _create(client, labels=["self"], nominee="a@t").json()
    ver = client.get(f"/issues/{i['id']}").json()["version"]
    r = client.patch(f"/issues/{i['id']}", json={"state": "todo", "version": ver})
    assert r.status_code == 409, "승격 게이트 — 사람/트리아지만"
    assert "promoted" in str(r.json()["detail"])
    r2 = client.patch(f"/issues/{i['id']}", json={"state": "todo", "version": ver,
                                                  "promoted": True})
    assert r2.status_code == 200 and r2.json()["state"] == "todo"


def test_일반_카드는_영향_없다(client):
    r = _create(client, labels=["auto"], state="todo")
    assert r.status_code == 201 and r.json()["state"] == "todo"
    assert r.json()["assignee"] == ""
    # promoted 없이도 todo 유지
    i = r.json()
    ver = client.get(f"/issues/{i['id']}").json()["version"]
    r2 = client.patch(f"/issues/{i['id']}", json={"state": "backlog", "version": ver})
    assert r2.status_code == 200
    ver = r2.json()["version"]
    r3 = client.patch(f"/issues/{i['id']}", json={"state": "todo", "version": ver})
    assert r3.status_code == 200


def test_WIP_카운트는_nominee_기준_독립적이다(client):
    """R1 회귀: 승격으로 assignee가 비워져도 nominee 기준 WIP는 유지된다."""
    i = _create(client, labels=["self"], nominee="a@t").json()
    ver = client.get(f"/issues/{i['id']}").json()["version"]
    client.patch(f"/issues/{i['id']}", json={"state": "todo", "version": ver,
                                             "promoted": True})
    assert client.get(f"/issues/{i['id']}").json()["assignee"] == ""
    r = _create(client, labels=["self"], nominee="a@t", title="둘째")
    assert r.status_code == 409


def test_승격_게이트는_요청_라벨을_본다(client):
    """R2 회귀: labels+state 동시 PATCH — self 라벨이 추가되면 게이트가 막는다."""
    r = _create(client, labels=["auto"], state="backlog")
    i = r.json()
    ver = client.get(f"/issues/{i['id']}").json()["version"]
    r2 = client.patch(f"/issues/{i['id']}", json={"labels": ["auto", "self"],
                                                  "state": "todo", "version": ver})
    assert r2.status_code == 409, "self 라벨이 되는 순간 승격 게이트 적용"
    r3 = client.patch(f"/issues/{i['id']}", json={"labels": ["auto", "self"],
                                                  "state": "todo", "version": ver,
                                                  "promoted": True})
    assert r3.status_code == 200
