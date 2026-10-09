"""TT 개선#3c — 교차리뷰 자동 수령.

유휴 에이전트가 타인의 review 대기 카드를 자동 claim-review한다(#3b policy 기반).
본인 작업 카드·본인 lease 카드·계약 없는 카드(토론·상징)는 제외.
"""
import pytest

import dispatchd
import policy
import probe.core

SHA8 = "b" * 8


def _i(**kw):
    base = {"id": "M4ABCDEF-GH12", "state": "review", "archived": 0, "assignee": "a1@t",
            "reviewer": "", "lease_by": "", "lease_expires": None, "labels": [],
            "dispatches": 1, "execution_attempt": 1,
            "work_contract": {"version": "v2"}, "comments": [],
            "updated_at": "2026-10-09T09:00:00+0900", "title": "M4ABCDEF-GH12"}
    base.update(kw)
    return base


NOW = "2026-10-09T10:00:00+0900"


# --- policy.review_eligible 매트�스 ---

def test_review_eligible_matrix():
    ok, why = policy.review_eligible(_i(), now=NOW, agent="a2@t")
    assert ok is True and why is None
    assert policy.review_eligible(_i(assignee="a2@t"), now=NOW, agent="a2@t")[1] == "own_work"
    held = dict(_i(), reviewer="r@t", lease_by="r@t",
                lease_expires="2026-10-09T11:00:00+0900")
    assert policy.review_eligible(held, now=NOW, agent="a2@t")[1] == "review_occupied"
    # 점유자 본인은 유지 가능(재개)
    assert policy.review_eligible(held, now=NOW, agent="r@t")[0] is True
    assert policy.review_eligible(_i(work_contract=None), now=NOW, agent="a2@t")[1] == "no_contract"
    assert policy.review_eligible(_i(state="done"), now=NOW, agent="a2@t")[1] == "state:terminal"
    assert policy.review_eligible(_i(state="todo"), now=NOW, agent="a2@t")[1] == "state:todo"


# --- decide: review-claim 액션 ---

def _snap(issues, prs=(), agents=()):
    return {"auto": True, "now": NOW, "agents": list(agents),
            "issues": list(issues), "prs": list(prs)}


def _pr(number=9, sha="b" * 40):
    return [{"number": number, "repo": "plainOldCode/think-tank",
             "checks": [{"state": "SUCCESS"}],
             "branch": "tt/M4ABCDEF-GH12-x", "title": "", "head_sha": sha}]


def _agent(name):
    return {"name": name, "enabled": True, "base_url": "http://x"}


@pytest.fixture(autouse=True)
def grace_off(monkeypatch):
    monkeypatch.setenv("TT_REVIEW_GRACE_MIN", "0")


def test_유휴_타인_에이전트가_review_카드를_자동_수령한다(monkeypatch):
    monkeypatch.setenv("TT_REVIEW_AGENT", "fallback@t")
    acts = dispatchd.decide(_snap([_i()], _pr(), [_agent("a2@t")]))
    claims = [a for a in acts if a["action"] == "review-claim"]
    assert len(claims) == 1 and claims[0]["agent"] == "a2@t"
    assert claims[0]["issue"] == "M4ABCDEF-GH12"
    # 수령이 이뤄지면 환경 리뷰어 dispatch(review-request)는 안 나간다
    assert not [a for a in acts if a["action"] == "review-request"]


def test_본인_작업_카드는_수령하지_않는다(monkeypatch):
    monkeypatch.setenv("TT_REVIEW_AGENT", "fallback@t")
    acts = dispatchd.decide(_snap([_i(assignee="a2@t")], _pr(), [_agent("a2@t")]))
    assert not [a for a in acts if a["action"] == "review-claim"]
    # 대체 경로: 기존 review-request로 환경 리뷰어에게 간다
    assert [a for a in acts if a["action"] == "review-request"]


def test_계약없는_카드는_수령하지_않는다(monkeypatch):
    monkeypatch.setenv("TT_REVIEW_AGENT", "fallback@t")
    acts = dispatchd.decide(_snap([_i(work_contract=None)], _pr(), [_agent("a2@t")]))
    assert not [a for a in acts if a["action"] == "review-claim"]


def test_활성_리뷰어_점유면_수령_없음(monkeypatch):
    monkeypatch.setenv("TT_REVIEW_AGENT", "fallback@t")
    held = _i(reviewer="r@t", lease_by="r@t", lease_expires="2026-10-09T11:00:00+0900")
    acts = dispatchd.decide(_snap([held], _pr(), [_agent("a2@t")]))
    assert not [a for a in acts if a["action"] == "review-claim"]


def test_유휴_에이전트_없으면_기존_review_request(monkeypatch):
    monkeypatch.setenv("TT_REVIEW_AGENT", "fallback@t")
    acts = dispatchd.decide(_snap([_i()], _pr(), []))
    assert not [a for a in acts if a["action"] == "review-claim"]
    assert [a for a in acts if a["action"] == "review-request"]


# --- execute: review-claim ---

def test_execute_review_claim은_점유후_dispatch한다(monkeypatch):
    calls = []

    def fake_api(url, path, method="GET", body=None):
        calls.append((method, path, body))
        if path.endswith("/claim-review"):
            return {"id": "M4X"}
        return {"comments": [], "version": 3}

    monkeypatch.setattr(probe.core, "api", fake_api)
    probe.core.execute("http://x", {"action": "review-claim", "agent": "a2@t",
                                    "issue": "M4ABCDEF-GH12", "pr": 9, "repo": "o/r",
                                    "head_sha": "b" * 40,
                                    "marker": "[review-req #9/" + "b" * 8 + "]"})
    assert ("POST", "/issues/M4ABCDEF-GH12/claim-review", {"agent": "a2@t"}) in calls
    disp = [b for m, p, b in calls if p == "/issues/M4ABCDEF-GH12/dispatch"]
    assert disp and disp[0]["agent"] == "a2@t"
    assert disp[0]["work_contract"] == probe.core.REVIEW_CONTRACT


def test_review_claim_e2e_본인_작업은_서버가_거부한다(client):
    """본인 작업 제외 확인 — 서버 claim-review가 own_work를 409로 막는다(e2e 접점)."""
    i = client.post("/issues", json={"title": "교차리뷰 e2e",
                                     "acceptance": "완료 기준: 본인 수령 거부"}).json()
    client.post(f"/issues/{i['id']}/claim", json={"agent": "a1@t"})
    ver = client.get(f"/issues/{i['id']}").json()["version"]
    client.patch(f"/issues/{i['id']}", json={"state": "review", "version": ver})
    r = client.post(f"/issues/{i['id']}/claim-review", json={"agent": "a1@t"})
    assert r.status_code == 409 and "본인 작업" in r.json()["detail"]
    r2 = client.post(f"/issues/{i['id']}/claim-review", json={"agent": "a2@t"})
    assert r2.status_code == 200 and r2.json()["reviewer"] == "a2@t"
