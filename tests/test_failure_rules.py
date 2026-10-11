"""R1 규칙표 데이터화 (TT M4K86Z3A-XS06) — §4 데이터 모듈 + 배선 행 테스트.

pr_no_checks: checks 0건 PR — 유효 판정 있으면 그 판정을 따르고, 무판정이면 리뷰 요청,
게이트 off 레거시 모드는 기존 무응답 유지.
draft_merge_loop: ready 요청 후 경과 시 사람 1회 통보(dedup).
"""
import pytest

import dispatchd
import failure_rules

SHA8 = "b" * 8
NOW = "2026-10-09T10:00:00+0900"
OLD = "2026-10-09T09:00:00+0900"


def _i(**kw):
    base = {"id": "M4ABCDEF-GH12", "state": "review", "archived": 0, "version": 3,
            "assignee": "a1@t",
            "reviewer": "", "lease_by": "", "lease_expires": None, "labels": [],
            "dispatches": 1, "execution_attempt": 1,
            "work_contract": {"version": "v2"}, "comments": [],
            "updated_at": "2026-10-09T09:00:00+0900", "title": "M4ABCDEF-GH12"}
    base.update(kw)
    return base


def _snap(issues, prs=(), agents=()):
    return {"auto": True, "now": NOW, "agents": list(agents),
            "issues": list(issues), "prs": list(prs)}


def _pr(number=9, sha="b" * 40, checks=None, draft=False):
    return [{"number": number, "repo": "plainOldCode/think-tank",
             "checks": [{"state": "SUCCESS"}] if checks is None else checks,
             "isDraft": draft,
             "branch": "tt/M4ABCDEF-GH12-x", "title": "", "head_sha": sha}]


def _agent(name):
    return {"name": name, "enabled": True, "base_url": "http://x"}


def _pc(body, ts=OLD, author="probe"):
    return {"author": author, "body": body, "ts": ts}


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("TT_REVIEW_GRACE_MIN", "0")
    monkeypatch.setenv("TT_REVIEW_AGENT", "fallback@t")
    monkeypatch.delenv("TT_DRAFT_STALL_H", raising=False)


# --- 데이터 모듈 ---

def test_규칙표는_필수_키를_가진다():
    rows = failure_rules.rules()
    codes = {r["code"] for r in rows}
    # §4 15행 + reviewer_session_failed(§12 신규)
    assert {"no_pr", "pr_no_checks", "ci_failed", "merged_no_report", "fix_dispatch_404",
            "reviewer_session_failed", "evidence_rejected", "verify_422", "runner_stall",
            "budget_exhausted", "needs_decision", "lease_conflict_401", "draft_merge_loop",
            "dependency", "marker_mismatch", "stale_base", "unknown"} <= codes
    for r in rows:
        assert r["handling"] in {"R", "R+S", "R+C", "H", "BUG", "C", "미확인"}
        assert r["judgment"] and isinstance(r["wired"], str)


def test_미등록_코드는_unknown_처리를_따른다():
    assert failure_rules.handling_of("never_seen") == "C"
    assert failure_rules.handling_of("budget_exhausted") == "R"


# --- pr_no_checks 배선 ---

def test_checks_0건_무판정이면_리뷰_요청한다():
    acts = dispatchd.decide(_snap([_i()], _pr(checks=[]), [_agent("a2@t")]))
    reqs = [a for a in acts if a["action"] == "review-request"]
    assert len(reqs) == 1 and reqs[0]["pr"] == 9
    assert "리뷰 요청" in reqs[0]["reason"]
    assert not [a for a in acts if a["action"] == "merge"]


def test_checks_관측_불가_None은_판정_대상이_아니다(monkeypatch):
    # 리뷰 R1 — CI 조회 실패(checks=None)는 0건과 다르다: 병합 우회 금지
    monkeypatch.setenv("TT_REVIEW_AGENT", "fallback@t")
    i = _i(reviewer="fallback@t",
           comments=[_pc("review: approve PR#9@bbbbbbbb — ok", author="fallback@t")])
    acts = dispatchd.decide(_snap([i], [{"number": 9, "repo": "plainOldCode/think-tank",
                                         "checks": None, "isDraft": False,
                                         "branch": "tt/M4ABCDEF-GH12-x", "title": "",
                                         "head_sha": "b" * 40}], [_agent("a2@t")]))
    assert not [a for a in acts if a["action"] == "merge"]
    assert not [a for a in acts if a["action"] == "review-request"]


def test_행별_처리_구분_골든():
    # 각 행의 code→handling 매핑이 데이터와 일치(행 추가 시 여기도 갱신)
    golden = {"no_pr": "R+C", "pr_no_checks": "R", "ci_failed": "R+S",
              "merged_no_report": "R", "fix_dispatch_404": "BUG",
              "reviewer_session_failed": "R", "evidence_rejected": "R+S",
              "verify_422": "R", "runner_stall": "R", "budget_exhausted": "R",
              "needs_decision": "H", "lease_conflict_401": "R",
              "draft_merge_loop": "R", "dependency": "H",
              "marker_mismatch": "미확인", "stale_base": "미확인", "unknown": "C"}
    for code, h in golden.items():
        assert failure_rules.handling_of(code) == h, code


def test_checks_0건_승인_판정이면_병합_후보다():
    i = _i(reviewer="fallback@t",
           comments=[_pc("review: approve PR#9@bbbbbbbb — ok", author="fallback@t")])
    acts = dispatchd.decide(_snap([i], _pr(checks=[]), [_agent("a2@t")]))
    m = [a for a in acts if a["action"] == "merge"]
    assert len(m) == 1 and "checks 0건" in m[0]["reason"]


def test_checks_0건_게이트_꺼짐은_기존_무응답_유지(monkeypatch):
    monkeypatch.delenv("TT_REVIEW_AGENT", raising=False)
    acts = dispatchd.decide(_snap([_i()], _pr(checks=[]), []))
    assert not [a for a in acts if a["action"] in ("merge", "review-request")]


# --- draft_merge_loop 배선 ---

def test_draft_신규는_ready_요청만_한다():
    acts = dispatchd.decide(_snap([_i()], _pr(draft=True), []))
    notes = [a for a in acts if a["action"] == "review-note"]
    assert len(notes) == 1 and "draft-flagged" in notes[0]["marker"]
    assert not [a for a in acts if a["action"] == "needs-human"]


def test_draft_경과시_사람_1회_통보한다(monkeypatch):
    monkeypatch.setenv("TT_DRAFT_STALL_H", "1")  # OLD=1h 전 — 임계 도달
    i = _i(comments=[_pc("[draft-flagged #9] draft PR#9 — ready for review 요청")])
    acts = dispatchd.decide(_snap([i], _pr(draft=True), []))
    nh = [a for a in acts if a["action"] == "needs-human"]
    assert len(nh) == 1 and "needs-human-draft #9" in nh[0]["note"]
    # dedup — 이미 통보됐으면 반복 없음
    i2 = _i(comments=[_pc("[draft-flagged #9] x"),
                      _pc("[needs-human-draft #9] 사람 판단 대기", author="dispatchd")])
    acts = dispatchd.decide(_snap([i2], _pr(draft=True), []))
    assert not [a for a in acts if a["action"] == "needs-human"]


# --- evidence_rejected 배선 (리뷰 R3: 서버가 DONE_GATE_MSG 시스템 코멘트로 기록함) ---

def test_done_거부_기록_카드는_재제출_요청_노트를_낸다():
    i = _i(state="in_progress",
           comments=[_pc("done 증거 없음 또는 실패/미확정 보고 — 현재 회차의 성공 결과가 필요합니다.",
                         author="tt-server")])
    acts = dispatchd.decide(_snap([i], [], []))
    notes = [a for a in acts if a["action"] == "review-note"]
    assert len(notes) == 1 and notes[0]["marker"] == "[evidence-rejected a1]"
    assert "재제출" in notes[0]["reason"]


def test_done_거부_기록이_없거나_완료된_카드는_무음이다():
    acts = dispatchd.decide(_snap([_i(state="in_progress")], [], []))
    assert not [a for a in acts if a["action"] == "review-note"]
    i = _i(state="done", comments=[_pc("done 증거 없음", author="tt-server")])
    acts = dispatchd.decide(_snap([i], [], []))
    assert not [a for a in acts if a["action"] == "review-note"]


# --- draft × needs-merge 상호작용 (리뷰 R2) ---

def test_draft_통보는_구_needs_merge_마커에_가려지지_않는다():
    # [needs-merge a1]이 있어도 draft-flagged 경과 시 사람 통보가 나와야 한다
    i = _i(comments=[_pc("[needs-merge a1] PR 없음 — 사람 판단 대기"),
                     _pc("[draft-flagged #9] draft PR#9 — ready for review 요청",
                         ts="2026-10-06T09:00:00+0900")])  # 3일 전 — 48h 임계 경과
    acts = dispatchd.decide(_snap([i], _pr(draft=True), []))
    nh = [a for a in acts if a["action"] == "needs-human"]
    assert len(nh) == 1 and "needs-human-draft #9" in nh[0]["note"]
    assert not [a for a in acts if a["action"] == "review-note"]
