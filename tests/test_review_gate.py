"""리뷰 게이트 (M428RMBY-XC0K): TT_REVIEW_AGENT 설정 시 CI green PR도 리뷰 승인 코멘트가 있어야 merge.

docs/review-gate.md v0.1 — TT 코멘트 첫 줄 'review: approve|request-changes',
둘째 줄 'PR#<n>@<sha8>'(현재 PR head와 불일치면 stale=무효).
- 미리뷰/stale → [review-req #pr/sha8] 마커 1회 review-request(리뷰어 dispatch)
- request-changes → [review-fix] 마커 1회 review-fix(카드 review 반납+수정 dispatch)
- approve(현재 head 유효) → 기존 merge 경로
- TT_REVIEW_AGENT 미설정 → 게이트 off(기존 동작 유지)
"""
import pytest

import dispatchd
import probe.core

REV = "kanban-adapter"
SHA8 = "b" * 8


@pytest.fixture
def gate_on(monkeypatch):
    monkeypatch.setenv("TT_REVIEW_AGENT", REV)


def _issue(state="review", iid="M428RMBY-XC0K", attempt=1, comments=(), assignee="a@t"):
    return {"id": iid, "state": state, "assignee": assignee, "lease_expires": None,
            "labels": [], "priority": None, "execution_attempt": attempt,
            "dispatches": 1, "release_ready": False, "waiting_for": None,
            "work_contract": {"version": "v2"}, "comments": list(comments),
            "updated_at": "2026-10-04T09:00:00+0900", "title": iid}


def _snap(issues, prs=()):
    return {"auto": True, "now": "2026-10-04T10:00:00+0900", "agents": [],
            "issues": list(issues), "prs": list(prs)}


def _pr(number=9, sha="b" * 40):
    return [{"number": number, "repo": "plainOldCode/think-tank",
             "checks": [{"state": "SUCCESS"}],
             "branch": "tt/M428RMBY-XC0K-x", "title": "", "head_sha": sha}]


# --- decide: 게이트 off ---

def test_게이트off는_기존동작_무리뷰_merge():
    acts = dispatchd.decide(_snap([_issue()], _pr()))
    assert [a for a in acts if a["action"] == "merge"]
    assert not [a for a in acts if a["action"] in ("review-request", "review-fix")]


# --- decide: 미리뷰 / stale ---

def test_미리뷰는_review_request_액션(gate_on):
    acts = dispatchd.decide(_snap([_issue()], _pr()))
    reqs = [a for a in acts if a["action"] == "review-request"]
    assert len(reqs) == 1
    assert reqs[0]["marker"] == f"[review-req #9/{SHA8}]"
    assert reqs[0]["pr"] == 9
    assert not [a for a in acts if a["action"] == "merge"]


def test_같은head_재요청_금지(gate_on):
    i = _issue(comments=[{"author": "probe", "body": f"[review-req #9/{SHA8}] 리뷰 요청"}])
    acts = dispatchd.decide(_snap([i], _pr()))
    assert not [a for a in acts if a["action"] in ("review-request", "merge")]


def test_stale_리뷰는_무효_재요청(gate_on):
    i = _issue(comments=[{"author": REV, "body": "review: approve\nPR#9@aaaaaaaa ok"}])
    acts = dispatchd.decide(_snap([i], _pr()))
    assert [a for a in acts if a["action"] == "review-request"]


def test_pr번호_불일치_리뷰도_stale_재요청(gate_on):
    # sha8 일치해도 PR#이 다르면 무효 — stale fail-safe(t_c69b13f0 F3).
    i = _issue(comments=[{"author": REV, "body": f"review: approve\nPR#8@{SHA8} ok"}])
    acts = dispatchd.decide(_snap([i], _pr()))
    assert [a for a in acts if a["action"] == "review-request"]
    assert not [a for a in acts if a["action"] == "merge"]


def test_다른리뷰어_코멘트는_무시(gate_on):
    i = _issue(comments=[{"author": "random-bot", "body": f"review: approve\nPR#9@{SHA8}"}])
    acts = dispatchd.decide(_snap([i], _pr()))
    assert [a for a in acts if a["action"] == "review-request"]


# --- decide: approve / request-changes ---

def test_approve면_merge(gate_on):
    i = _issue(comments=[{"author": REV, "body": f"review: approve\nPR#9@{SHA8} lgtm"}])
    acts = dispatchd.decide(_snap([i], _pr()))
    assert len([a for a in acts if a["action"] == "merge"]) == 1
    assert not [a for a in acts if a["action"] in ("review-request", "review-fix")]


def test_request_changes면_review_fix(gate_on):
    i = _issue(comments=[{"author": REV, "body": f"review: request-changes\nPR#9@{SHA8} 시크릿"}])
    acts = dispatchd.decide(_snap([i], _pr()))
    fx = [a for a in acts if a["action"] == "review-fix"]
    assert len(fx) == 1
    assert fx[0]["marker"] == f"[review-fix #9/{SHA8}]"
    assert not [a for a in acts if a["action"] == "merge"]


def test_review_fix_중복_금지(gate_on):
    i = _issue(comments=[
        {"author": REV, "body": f"review: request-changes\nPR#9@{SHA8} 시크릿"},
        {"author": "probe", "body": f"[review-fix #9/{SHA8}] 반납"}])
    acts = dispatchd.decide(_snap([i], _pr()))
    assert not [a for a in acts if a["action"] == "review-fix"]


def test_게이트카드는_needs_merge_소음_제외(gate_on):
    # review 카드 + green PR + 미리뷰 → review-request만. ⓔb 노트 전무(docs/review-gate.md:56).
    acts = dispatchd.decide(_snap([_issue()], _pr()))
    assert [a for a in acts if a["action"] == "review-request"]
    # review-note 전무 단언 — '"PR 없음" in reason' 필터는 게이트 카드의 실제 소음
    # ('PR green — 병합 판정 제외됨…')을 못 잡아 '| gated' 제거 뮤테이션에서도 GREEN이었음(t_c69b13f0).
    assert not [a for a in acts if a["action"] == "review-note"]


# --- review_verdict: 유효 판정 2건 — 최신 우선 ---

def test_review_verdict는_최신_판정_우선():
    # 리뷰어가 판정을 2회 남기면 배열 뒤(최신) 판정이 이김 — TT 코멘트는 id 오름차순(F2 실츬).
    old = {"author": REV, "body": f"review: approve\nPR#9@{SHA8} lgtm"}
    new = {"author": REV, "body": f"review: request-changes\nPR#9@{SHA8} 수정 필요"}
    assert probe.core.review_verdict([old, new], REV, 9, SHA8) == "request-changes"
    assert probe.core.review_verdict([new, old], REV, 9, SHA8) == "approve"


# --- execute ---

class Rec:
    """api 더블 — GET 카드 반환, dispatch/코멘트 기록, PATCH 기록."""

    def __init__(self, card, dispatch_fail=False):
        self.card = card
        self.dispatches = []
        self.commented = []
        self.patched = []
        self.fail = dispatch_fail

    def __call__(self, url, path, method="GET", body=None):
        if method == "GET":
            return dict(self.card)
        if method == "PATCH":
            self.patched.append(body)
            return {**self.card, "state": body.get("state")}
        if path.endswith("/dispatch") and method == "POST":
            if self.fail:
                raise RuntimeError("no receiver")
            self.dispatches.append(body)
            return {"ok": True}
        self.commented.append(body)
        return {"ok": True}


def _review_req_act():
    return {"agent": "probe", "issue": "M428RMBY-XC0K", "action": "review-request",
            "pr": 9, "repo": "plainOldCode/think-tank", "branch": "tt/x",
            "head_sha": "b" * 40, "marker": f"[review-req #9/{SHA8}]", "reason": "r"}


def test_execute_review_request는_리뷰어_dispatch와_마커(gate_on, monkeypatch):
    card = {"id": "M428RMBY-XC0K", "state": "review", "version": 3, "comments": []}
    rec = Rec(card)
    monkeypatch.setattr(probe.core, "api", rec)
    probe.core.execute("u", _review_req_act())
    assert len(rec.dispatches) == 1
    assert rec.dispatches[0]["agent"] == REV
    assert "review: approve" in rec.dispatches[0]["message"]
    assert any(f"[review-req #9/{SHA8}]" in c["body"]
               for c in rec.commented if c.get("author") == "probe")


def test_execute_review_request_dispatch실패는_사람판단_코멘트(gate_on, monkeypatch):
    card = {"id": "M428RMBY-XC0K", "state": "review", "version": 3, "comments": []}
    rec = Rec(card, dispatch_fail=True)
    monkeypatch.setattr(probe.core, "api", rec)
    probe.core.execute("u", _review_req_act())
    assert not rec.dispatches
    assert any("dispatch 실패" in c["body"] for c in rec.commented)
    assert not any(f"[review-req #9/{SHA8}] PR#9" in c["body"] for c in rec.commented)


def _review_fix_act():
    return {"agent": "probe", "issue": "M428RMBY-XC0K", "action": "review-fix",
            "pr": 9, "repo": "plainOldCode/think-tank", "branch": "tt/x",
            "head_sha": "b" * 40, "marker": f"[review-fix #9/{SHA8}]", "reason": "r"}


def test_execute_review_fix는_review반납_수정_dispatch(gate_on, monkeypatch):
    card = {"id": "M428RMBY-XC0K", "state": "in_progress", "version": 5,
            "assignee": "a@t", "comments": []}
    rec = Rec(card)
    monkeypatch.setattr(probe.core, "api", rec)
    probe.core.execute("u", _review_fix_act())
    assert {"version": 5, "state": "review"} in rec.patched
    assert rec.dispatches[0]["agent"] == "a@t"
    assert "request-changes" in rec.dispatches[0]["message"]
    assert any(f"[review-fix #9/{SHA8}]" in c["body"]
               for c in rec.commented if c.get("author") == "probe")


def test_execute_review_fix_배정자없으면_사람판단_코멘트(gate_on, monkeypatch):
    card = {"id": "M428RMBY-XC0K", "state": "review", "version": 5,
            "assignee": "", "comments": []}
    rec = Rec(card)
    monkeypatch.setattr(probe.core, "api", rec)
    probe.core.execute("u", _review_fix_act())
    assert not rec.dispatches
    assert any("배정 에이전트 없음" in c["body"] for c in rec.commented)


# --- hydrate (run_once 배선) ---

def test_hydrate_reviews는_후보카드_코멘트만_취득(monkeypatch):
    got = []

    def fake_api(url, path, method="GET", body=None):
        got.append(path)
        return {"comments": [{"author": REV, "body": f"review: approve\nPR#9@{SHA8}"}]}

    monkeypatch.setattr(probe.core, "api", fake_api)
    i = _issue()
    del i["comments"]  # snapshot이 review 카드가 아니면 코멘트 미취득
    snap = _snap([i], _pr())
    probe.core.hydrate_reviews("u", snap)
    assert snap["issues"][0]["comments"], "merge 후보 카드 코멘트 취득"
    assert f"/issues/M428RMBY-XC0K" in got


def test_hydrate_reviews는_비후보카드를_GET하지_않음(monkeypatch):
    # PR에 묶이지 않은 카드(비후보)는 GET 금지(t_c69b13f0 F4) — comments 키도 추가하지 않음.
    got = []

    def fake_api(url, path, method="GET", body=None):
        got.append(path)
        return {"comments": []}

    monkeypatch.setattr(probe.core, "api", fake_api)
    i = _issue(iid="M428RMMX-OTHER", state="in_progress")
    del i["comments"]
    snap = _snap([i], _pr())  # PR은 M428RMBY-XC0K에만 연결
    probe.core.hydrate_reviews("u", snap)
    assert not got, "비후보 카드는 GET하지 않음"
    assert "comments" not in snap["issues"][0]


def test_hydrate_reviews_api_실패시_빈코멘트_폴백(monkeypatch):
    # 후보 카드 GET 실패 시 comments=[] 폴백 — run_once는 hydrate를 무방비 호출하므로
    # 예외 누출 시 사이클 전체가 죽는다(t_c69b13f0 F4).
    def fake_api(url, path, method="GET", body=None):
        raise RuntimeError("tt unreachable")

    monkeypatch.setattr(probe.core, "api", fake_api)
    i = _issue()
    del i["comments"]
    snap = _snap([i], _pr())
    probe.core.hydrate_reviews("u", snap)
    assert snap["issues"][0]["comments"] == []


def test_run_once는_게이트_on시_하이드레이트_선행(gate_on, monkeypatch):
    order = []

    def fake_api(url, path, method="GET", body=None):
        order.append(("api", path))
        return {"comments": []}

    monkeypatch.setattr(probe.core, "api", fake_api)
    # snapshot은 review 카드가 아니면 코멘트를 안 취득 — comments 키 없는 이슈로 반환
    monkeypatch.setattr(
        probe.core, "snapshot",
        lambda url: {"auto": True, "now": "", "agents": [],
                     "issues": [{k: v for k, v in _issue().items() if k != "comments"}]})
    monkeypatch.setattr(probe.core, "collect_prs", lambda repos: _pr())

    def fake_decide(snap):
        order.append(("decide", "comments" in snap["issues"][0]))
        return []

    monkeypatch.setattr(probe.core, "decide", fake_decide)
    probe.core.run_once("u")
    assert ("decide", True) in order, "decide 전에 코멘트가 채워져야 함"
