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


def _issue(state="review", iid="M428RMBY-XC0K", attempt=1, comments=(), assignee="a@t",
           busy_agents=None):
    return {"id": iid, "state": state, "assignee": assignee, "version": 3, "lease_expires": None,
            "labels": [], "priority": None, "execution_attempt": attempt,
            "dispatches": 1, "release_ready": False, "waiting_for": None,
            "busy_agents": list(busy_agents or []),
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

    def __init__(self, card, dispatch_fail=False, agents=()):
        self.card = card
        self.agents = list(agents)
        self.dispatches = []
        self.commented = []
        self.patched = []
        self.claims = []  # probe가 대신 거는 claim-review(코멘트 아님)
        self.fail = dispatch_fail

    def __call__(self, url, path, method="GET", body=None):
        if method == "GET" and path.endswith("/agents"):
            return [dict(a) for a in self.agents]
        if method == "GET":
            return dict(self.card)
        if method == "PATCH":
            self.patched.append(body)
            return {**self.card, "state": body.get("state")}
        if path.endswith("/claim-review") and method == "POST":
            self.claims.append(body)
            return {"ok": True}
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
    rec = Rec(card, agents=[{"name": "a@t", "enabled": True, "base_url": "http://x", "receive_mode": "hook"}])
    monkeypatch.setattr(probe.core, "api", rec)
    probe.core.execute("u", _review_fix_act())
    assert {"version": 5, "state": "review"} in rec.patched
    assert rec.dispatches[0]["agent"] == "a@t"
    assert "request-changes" in rec.dispatches[0]["message"]
    assert any(f"[review-fix #9/{SHA8}]" in c["body"]
               for c in rec.commented if c.get("author") == "probe")


# --- R4(6XKC): review-fix dispatch 수신자 가족 해석 — fix_dispatch_404 제거 ---

def test_execute_review_fix는_식별코드_assignee를_등록명으로_dispatch한다(gate_on, monkeypatch):
    """acceptance ① — assignee=skshim@tp-13 → 등록 에이전트 opencode-tp13."""
    card = {"id": "M428RMBY-XC0K", "state": "in_progress", "version": 5,
            "assignee": "skshim@tp-13", "comments": []}
    rec = Rec(card, agents=[{"name": "opencode-tp13", "enabled": True,
                             "base_url": "http://x"}])
    monkeypatch.setattr(probe.core, "api", rec)
    probe.core.execute("u", _review_fix_act())
    assert rec.dispatches[0]["agent"] == "opencode-tp13"
    assert {"version": 5, "state": "review"} in rec.patched


def test_execute_review_fix는_정확명을_가족해석보다_우선한다(gate_on, monkeypatch):
    card = {"id": "M428RMBY-XC0K", "state": "in_progress", "version": 5,
            "assignee": "codex", "comments": []}
    rec = Rec(card, agents=[{"name": "codex", "enabled": True, "base_url": "http://x", "receive_mode": "hook"},
                            {"name": "claude", "enabled": True, "base_url": "http://x", "receive_mode": "hook"}])
    monkeypatch.setattr(probe.core, "api", rec)
    probe.core.execute("u", _review_fix_act())
    assert rec.dispatches[0]["agent"] == "codex"


def test_execute_review_fix는_해석실패시_등록_실행자에게_재위임한다(gate_on, monkeypatch):
    """M5283HM4-0SZP — 원작업자 고정 해제: 가족이 없어도 등록 실행자가 있으면
    대체 위임(통합 brief). 실행자가 전원 없을 때만 사람 통보로 종결한다."""
    card = {"id": "M428RMBY-XC0K", "state": "in_progress", "version": 5,
            "assignee": "skshim@tp-13", "comments": []}
    rec = Rec(card, agents=[{"name": "claude", "enabled": True, "base_url": "http://x", "receive_mode": "hook"}])
    monkeypatch.setattr(probe.core, "api", rec)
    probe.core.execute("u", _review_fix_act())
    assert rec.dispatches and rec.dispatches[0]["agent"] == "claude"
    assert any("claude" in c["body"] and "재위임" in c["body"] for c in rec.commented)
    assert {"version": 5, "state": "review"} in rec.patched


def test_execute_review_fix는_실행자_전원_부재시_사람통보로_종결한다(gate_on, monkeypatch):
    """대체 후보가 하나도 없으면 기존 경로 유지 — dispatch 시도 없이 사람 통보."""
    card = {"id": "M428RMBY-XC0K", "state": "in_progress", "version": 5,
            "assignee": "skshim@tp-13", "comments": []}
    rec = Rec(card, agents=[])
    monkeypatch.setattr(probe.core, "api", rec)
    probe.core.execute("u", _review_fix_act())
    assert not rec.dispatches
    assert any("에이전트 미등록: skshim@tp-13" in c["body"] and "사람 판단 대기" in c["body"]
               for c in rec.commented)
    assert {"version": 5, "state": "review"} in rec.patched


def test_execute_review_fix는_비활성_에이전트로_해석하지_않는다(gate_on, monkeypatch):
    card = {"id": "M428RMBY-XC0K", "state": "in_progress", "version": 5,
            "assignee": "skshim@tp-13", "comments": []}
    rec = Rec(card, agents=[{"name": "opencode-tp13", "enabled": False,
                             "base_url": "http://x"}])
    monkeypatch.setattr(probe.core, "api", rec)
    probe.core.execute("u", _review_fix_act())
    assert not rec.dispatches
    assert any("에이전트 미등록" in c["body"] for c in rec.commented)


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


def test_게이트on은_review상태_카드만_판정(gate_on):
    # 제출 전(in_progress) 카드는 리뷰·병합 대상 아님 — 보고 제출 후 판정
    i = _issue(state="in_progress")
    acts = dispatchd.decide(_snap([i], _pr()))
    assert not [a for a in acts if a["action"] in ("review-request", "merge", "review-fix")]


def test_판정기록시_리뷰어_반납_액션(gate_on):
    # verdict가 기록된 review 카드 — 리뷰어 점유 반납 액션 발행(중복 없이 1회)
    i = _issue(state="review")
    i["reviewer"] = REV
    i["comments"] = [{"author": REV, "body": "review: approve\nPR#9@bbbbbbbb"}]
    acts = dispatchd.decide(_snap([i], _pr()))
    rel = [a for a in acts if a["action"] == "release-reviewer"]
    assert len(rel) == 1 and rel[0]["reviewer"] == REV


def test_release_reviewer_실행은_빈문자열과_expected_version으로_PATCH한다(gate_on, monkeypatch):
    # R1 회귀: reviewer=None PATCH는 핸들러가 무시(no-op) — 규약 일치 필수
    sent = []
    card = {"id": "X", "reviewer": REV, "assignee": "a@t", "version": 3,
            "comments": [{"author": REV, "body": "review: approve\nPR#9@bbbbbbbb"}]}
    monkeypatch.setattr(probe.core, "api",
                        lambda url, path, method="GET", payload=None:
                        sent.append((path, method, payload)) or card)
    dispatchd.execute("http://x", {"issue": "X", "action": "release-reviewer", "reviewer": REV,
                                   "pr": 9, "head_sha": "b" * 40, "expected_version": 3})
    path, method, payload = sent[-1]
    assert method == "PATCH" and path == "/issues/X"
    assert payload["reviewer"] == ""
    assert payload["expected_version"] == 3  # "version" 키는 모델에 없어 무시된다


def test_stale_반납_액션은_최신판정이_다르면_폐기된다(gate_on, monkeypatch):
    # R5 — 이전 head 판정의 반납 액션이 새 점유를 지우지 않게: 최신 판정 sha 불일치 시 no-op
    sent = []
    card = {"id": "X", "reviewer": REV, "assignee": "a@t", "version": 7,
            "comments": [{"author": REV, "body": "review: approve\nPR#9@cccccccc"}]}
    monkeypatch.setattr(probe.core, "api",
                        lambda url, path, method="GET", payload=None:
                        sent.append((path, method, payload)) or card)
    # 액션은 구 판정(bbbbbbbb) 기준 — 최신 판정(cccccccc)과 불일치 → 폐기
    probe.core.execute("http://x", {"issue": "X", "action": "release-reviewer",
                                    "reviewer": REV, "pr": 9, "head_sha": "b" * 40,
                                    "expected_version": 3})
    assert not [s for s in sent if s[1] == "PATCH"]


def test_반납_액션은_decide이후_버전변화면_폐기된다(gate_on, monkeypatch):
    # R5 변형 — 같은 판정 구간에서 리뷰어가 재claim(버전 bump)한 경우 stale 액션 스킵
    sent = []
    card = {"id": "X", "reviewer": REV, "assignee": "a@t", "version": 9,
            "comments": [{"author": REV, "body": "review: approve\nPR#9@bbbbbbbb"}]}
    monkeypatch.setattr(probe.core, "api",
                        lambda url, path, method="GET", payload=None:
                        sent.append((path, method, payload)) or card)
    probe.core.execute("http://x", {"issue": "X", "action": "release-reviewer",
                                    "reviewer": REV, "pr": 9, "head_sha": "b" * 40,
                                    "expected_version": 7})  # decide 시점 7 → 현재 9
    assert not [s for s in sent if s[1] == "PATCH"]


def test_반납은_리뷰어_판정만_인정한다(gate_on, monkeypatch):
    # R7 — 타 author 판정(러너 요약 인용 등)은 무시하고 리뷰어 판정으로만 반납 여부 결정
    sent = []
    card = {"id": "X", "reviewer": REV, "assignee": "a@t", "version": 3,
            "comments": [{"author": "runner", "body": "요약 인용\nreview: approve\nPR#9@bbbbbbbb"},
                         {"author": REV, "body": "review: approve\nPR#9@bbbbbbbb"}]}
    monkeypatch.setattr(probe.core, "api",
                        lambda url, path, method="GET", payload=None:
                        sent.append((path, method, payload)) or card)
    probe.core.execute("http://x", {"issue": "X", "action": "release-reviewer",
                                    "reviewer": REV, "pr": 9, "head_sha": "b" * 40,
                                    "expected_version": 3})
    assert [s for s in sent if s[1] == "PATCH"]  # 유효 판정 → 반납 실행


# --- M4N469TR-JWW0: 진행 중 dispatch가 있으면 리뷰/수정 중복 스폰 금지 ---

def test_진행중_dispatch있으면_새_리뷰요청_보류(gate_on):
    # 수동 재리뷰+자동 리뷰+자동 fix 동시 스폰 사고(PR#87 325~328) 방지
    i = _issue(busy_agents=["codex"])
    acts = dispatchd.decide(_snap([i], _pr()))
    assert not [a for a in acts if a["action"] in ("review-request", "review-claim")]
    assert not [a for a in acts if a["action"] == "merge"]


def test_진행중_dispatch없으면_정상_스폰(gate_on):
    i = _issue(busy_agents=[])
    acts = dispatchd.decide(_snap([i], _pr()))
    assert [a for a in acts if a["action"] == "review-request"]


def test_진행중_dispatch있으면_수정_스폰도_보류(gate_on):
    i = _issue(comments=[{"author": REV, "body": f"review: request-changes\nPR#9@{SHA8} 고칠 것"}],
               busy_agents=["codex"])
    acts = dispatchd.decide(_snap([i], _pr()))
    assert not [a for a in acts if a["action"] == "review-fix"]


def test_review_request_실행은_점유_후보를_제외한다(gate_on, monkeypatch):
    # 실행 직전 재확인 — decide 이후 상태 변화 방어(2중 가드)
    calls = []

    def fake_api(url, path, method="GET", body=None):
        calls.append((method, path))
        if path.endswith("/dispatches") and method == "GET":
            return [{"agent": REV, "run_state": "running"}]
        if path == "/issues/M428RMBY-XC0K":
            return _issue()
        return {}

    monkeypatch.setattr(probe.core, "api", fake_api)
    monkeypatch.setattr(probe.core, "get_version", lambda u, i: 3)
    act = {"agent": REV, "issue": "M428RMBY-XC0K", "action": "review-request", "pr": 9,
           "branch": "tt/x", "head_sha": SHA8, "repo": "plainOldCode/think-tank",
           "marker": f"[review-req #9/{SHA8}]"}
    probe.core.execute("http://x", act)
    assert not any(m == "POST" and p.endswith("/dispatch") for m, p in calls)


def test_review_request_실행은_부분_점유시_다음_후보에_수령한다(gate_on, monkeypatch):
    # 라우팅 리뷰어만 점유 중이면 REVIEW_FALLBACK 다음 후보로 claim이 나간다 —
    # 전원 점유가 아닌 한 스폰 금지가 아니다(리뷰 비블로커 테스트 공백).
    calls = []

    def fake_api(url, path, method="GET", body=None):
        calls.append((method, path, body))
        if path.endswith("/dispatches") and method == "GET":
            return [{"agent": "claude", "run_state": "running"}]
        if path.endswith("/claim-review"):
            return {"ok": True}
        if path == "/issues/M428RMBY-XC0K":
            return _issue()
        return {}

    monkeypatch.setattr(probe.core, "api", fake_api)
    probe.core.execute("http://x", {"agent": "probe", "issue": "M428RMBY-XC0K",
                                    "action": "review-request", "pr": 9,
                                    "repo": "plainOldCode/think-tank", "branch": "tt/x",
                                    "head_sha": SHA8, "marker": f"[review-req #9/{SHA8}]",
                                    "reviewer": "claude"})
    claims = [b for m, p, b in calls if p.endswith("/claim-review") and m == "POST"]
    assert claims and claims[0]["agent"] == "agy"
    assert not any(b.get("agent") == "claude" for b in claims)
    disp = [b for m, p, b in calls if p.endswith("/dispatch") and m == "POST"]
    assert len(disp) == 1 and disp[0]["agent"] == "agy"


def test_review_retry_실행은_정체_dispatch에_막히지_않는다(gate_on, monkeypatch):
    # 12MZ 재발령은 fresh 새 세션이므로 리뷰어의 정체(stalled) dispatch 점유와 무관하게
    # 나가야 한다. 가드에 걸리면 회차 마커가 남지 않아 REVIEW_RETRY_MAX와
    # [needs-human-review] 에스컬레이션까지 소리 없이 멈춘다(리뷰 블로커 재현 시나리오:
    # TT_REVIEW_AGENT 단일 후보 + /dispatches run_state=stalled).
    calls = []

    def fake_api(url, path, method="GET", body=None):
        calls.append((method, path, body))
        if path.endswith("/dispatches") and method == "GET":
            return [{"agent": REV, "run_state": "stalled"}]
        if path.endswith("/claim-review"):
            return {"ok": True}
        if path == "/issues/M428RMBY-XC0K":
            return _issue()
        return {}

    monkeypatch.setattr(probe.core, "api", fake_api)
    marker = f"[review-retry a1 #9/{SHA8}]"
    probe.core.execute("http://x", {"action": "review-retry", "agent": "probe",
                                    "issue": "M428RMBY-XC0K", "pr": 9,
                                    "repo": "plainOldCode/think-tank", "branch": "tt/x",
                                    "head_sha": SHA8, "marker": marker, "round": 1})
    disp = [b for m, p, b in calls if p.endswith("/dispatch") and m == "POST"]
    assert len(disp) == 1 and disp[0]["agent"] == REV
    assert disp[0]["fresh"] is True and marker in disp[0]["message"]
    # 회차 마커 영속화 — 카운터가 올라가 상한·에스컬레이션 경로가 살아있음
    comments = [b for m, p, b in calls if p.endswith("/comments") and m == "POST"]
    assert any(marker in (b.get("body") or "") for b in comments)


def test_review_fix_실행은_점유_대상에게_스폰하지_않는다(gate_on, monkeypatch):
    calls = []

    def fake_api(url, path, method="GET", body=None):
        calls.append((method, path, body))
        if path.endswith("/dispatches") and method == "GET":
            return [{"agent": "a@t", "run_state": "queued"}]
        if path == "/issues/M428RMBY-XC0K":
            return _issue()
        if path == "/agents":
            return [{"name": "a@t", "enabled": True, "base_url": "http://x", "receive_mode": "hook"}]
        return {}

    monkeypatch.setattr(probe.core, "api", fake_api)
    act = {"agent": "probe", "issue": "M428RMBY-XC0K", "action": "review-fix", "pr": 9,
           "branch": "tt/x", "head_sha": SHA8, "repo": "plainOldCode/think-tank",
           "marker": f"[review-fix #9/{SHA8}]"}
    probe.core.execute("http://x", act)
    assert not any(m == "POST" and p.endswith("/dispatch") for m, p, _ in calls)


def test_review_fix_대상이_실행불가_에이전트면_스폰하지_않는다(gate_on, monkeypatch):
    # M4NCNNRW-PXCJ — 어댑터로의 review-fix는 트리아지 카드만 만들고 정체(RTZ1 #343)
    calls = []

    def fake_api(url, path, method="GET", body=None):
        calls.append((method, path, body))
        if path == "/issues/M428RMBY-XC0K":
            return _issue(assignee="kanban-adapter")
        if path == "/agents":
            return [{"name": "kanban-adapter", "enabled": True, "base_url": "http://x", "receive_mode": "hook"}]
        return {}

    monkeypatch.setattr(probe.core, "api", fake_api)
    act = {"agent": "probe", "issue": "M428RMBY-XC0K", "action": "review-fix", "pr": 9,
           "branch": "tt/x", "head_sha": SHA8, "repo": "plainOldCode/think-tank",
           "marker": f"[review-fix #9/{SHA8}]"}
    probe.core.execute("http://x", act)
    assert not any(m == "POST" and p.endswith("/dispatch") for m, p, _ in calls)
    assert any(m == "POST" and p.endswith("/comments") and "실행 불가" in (b or {}).get("body", "")
               for m, p, b in calls)
    # 리뷰 R1 — 위임 불가인데 마감 코멘트가 "수정 위임"이면 직전 통보와 모순
    closing = [b for m, p, b in calls
               if m == "POST" and p.endswith("/comments") and "request-changes —" in (b or {}).get("body", "")]
    assert closing and "수정 위임 없음" in closing[-1]["body"]


def test_review_fix_가족해석은_실행불가를_건너뛴다(gate_on, monkeypatch):
    # PXCJ 리뷰 블로커 — hermes@ghost-a의 가족 해석이 등록 순서 첫 에이전트인
    # kanban-adapter(실행 불가)가 아니라 실행자(가족) ghost-b로 나가야 한다.
    calls = []

    def fake_api(url, path, method="GET", body=None):
        calls.append((method, path, body))
        if path == "/issues/M428RMBY-XC0K":
            return _issue(assignee="hermes@ghost-a")
        if path == "/agents":
            return [{"name": "kanban-adapter", "enabled": True, "base_url": "http://x", "receive_mode": "hook"},
                    {"name": "hermes@ghost-b", "enabled": True, "base_url": "http://x", "receive_mode": "hook"}]
        if path == "/issues/M428RMBY-XC0K/dispatches":
            return []
        return {}

    monkeypatch.setattr(probe.core, "api", fake_api)
    act = {"agent": "probe", "issue": "M428RMBY-XC0K", "action": "review-fix", "pr": 9,
           "branch": "tt/x", "head_sha": SHA8, "repo": "plainOldCode/think-tank",
           "marker": f"[review-fix #9/{SHA8}]"}
    probe.core.execute("http://x", act)
    disp = [b for m, p, b in calls if m == "POST" and p.endswith("/dispatch")]
    assert len(disp) == 1 and disp[0]["agent"] == "hermes@ghost-b"
    assert "request-changes" in disp[0]["message"]
    # 가족에 실행자가 있으면 사람 통보가 아니라 발령 마감 코멘트
    closing = [b for m, p, b in calls
               if m == "POST" and p.endswith("/comments") and "request-changes —" in (b or {}).get("body", "")]
    assert closing and "수정 위임" in closing[-1]["body"] and "실행 불가" not in closing[-1]["body"]


def test_resolve_executor는_정확_실행불가_일치를_그대로_돌린다():
    # 정확 일치가 NON_EXECUTOR면 통보 분기 판단용으로 그대로 돌린다(가족으로 대체하지 않음)
    agents = [{"name": "kanban-adapter"}, {"name": "hermes@ghost-b"}]
    assert probe.core._resolve_executor("kanban-adapter", agents) == "kanban-adapter"
    assert probe.core._resolve_executor("hermes@ghost-a", agents) == "hermes@ghost-b"
    # 가족에 실행자가 전혀 없으면 None — 사람 통보(에이전트 미등록)로 종결
    assert probe.core._resolve_executor("tt-reviewer@x", [{"name": "kanban-adapter"}]) is None
