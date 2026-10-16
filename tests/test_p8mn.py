"""P8MN(M5283KGX-P8MN) — 카드 brief 필드 정식 도입.

acceptance 옆에 SCOPE(쓸 수 있는/없는 경로)·VERIFY(정확한 명령)·FORBIDDEN·
TIMEBOX를 둔다. 빈 VERIFY는 auto 승격(라벨 부여)을 거부하고, auto pull에서도
건너뛴다(pstack: "missing fields are a refuse-to-spawn condition").
"""
import pytest


def _todo(client, **kw):
    kw.setdefault("acceptance", "완료 기준: 테스트 통과")
    return client.post("/issues", json={"title": "P8MN", **kw}).json()


# --- 저장·조회 ---

def test_brief_필드를_저장하고_조회한다(client):
    i = _todo(client, brief_scope="server/routers/issues.py 만", brief_verify="pytest tests/ -q",
              brief_forbidden="server/db.py 스키마 변경", brief_timebox="2h")
    b = client.get(f"/issues/{i['id']}").json()
    assert b["brief_scope"] == "server/routers/issues.py 만"
    assert b["brief_verify"] == "pytest tests/ -q"
    assert b["brief_forbidden"] == "server/db.py 스키마 변경"
    assert b["brief_timebox"] == "2h"


def test_brief_필드는_선택이고_patch로_갱신된다(client):
    i = _todo(client)
    assert client.get(f"/issues/{i['id']}").json()["brief_verify"] == ""
    r = client.patch(f"/issues/{i['id']}", json={"brief_verify": "pytest tests/test_p8mn.py -q",
                                                 "brief_timebox": "30m"})
    assert r.status_code == 200
    assert r.json()["brief_verify"] == "pytest tests/test_p8mn.py -q"


# --- auto 승격 거부 ---

def test_auto_라벨로_생성은_VERIFY가_필수다(client):
    r = client.post("/issues", json={"title": "t", "acceptance": "x", "labels": ["auto"]})
    assert r.status_code == 422 and "brief_verify" in r.json()["detail"]


def test_auto_라벨_생성은_VERIFY와_함께_가능하다(client):
    r = client.post("/issues", json={"title": "t", "acceptance": "x", "labels": ["auto"],
                                     "brief_verify": "pytest -q"})
    assert r.status_code == 201, r.text


def test_auto_라벨_추가도_VERIFY가_필수다(client):
    i = _todo(client)  # verify 없음
    r = client.patch(f"/issues/{i['id']}", json={"labels": ["auto"]})
    assert r.status_code == 422 and "brief_verify" in r.json()["detail"]
    # 같은 PATCH에서 verify를 함께 주면 통과
    r = client.patch(f"/issues/{i['id']}", json={"labels": ["auto"], "brief_verify": "pytest -q"})
    assert r.status_code == 200 and r.json()["brief_verify"] == "pytest -q"


def test_verify가_이미_있는_카드에_auto_라벨_추가는_가능하다(client):
    i = _todo(client, brief_verify="pytest -q")
    r = client.patch(f"/issues/{i['id']}", json={"labels": ["auto"]})
    assert r.status_code == 200


def test_auto가_아닌_라벨_변경은_VERIFY가_없어도_가능하다(client):
    i = _todo(client)
    r = client.patch(f"/issues/{i['id']}", json={"labels": ["improvement"]})
    assert r.status_code == 200


# --- 정책: auto pull은 빈 VERIFY 카드를 건너뛴다 ---

def test_auto_pull은_빈_VERIFY를_no_verify로_거부한다():
    from policy import busy_reason, eligible
    card = {"state": "todo", "labels": ["auto"], "brief_verify": "", "archived": 0}
    assert busy_reason(card, auto=True) == "no_verify"
    ok, reason = eligible(card, auto=True)
    assert not ok and reason == "no_verify"


def test_auto_pull은_VERIFY가_있으면_통과한다():
    from policy import busy_reason
    card = {"state": "todo", "labels": ["auto"], "brief_verify": "pytest -q", "archived": 0}
    assert busy_reason(card, auto=True) is None


def test_non_auto_모드는_VERIFY가_없어도_기존대로_판정한다():
    from policy import busy_reason
    card = {"state": "todo", "labels": [], "brief_verify": "", "archived": 0}
    assert busy_reason(card) is None  # auto 아님 — 기존 동작


def test_공백만_있는_VERIFY는_빈_것으로_판정한다():
    from policy import busy_reason
    card = {"state": "todo", "labels": ["auto"], "brief_verify": "   ", "archived": 0}
    assert busy_reason(card, auto=True) == "no_verify"
