"""R1 규칙표 데이터화 (TT M4K86Z3A-XS06) — §4 데이터 모듈 + 배선 행 테스트.

U4(M5A4HYY7-3BCA)로 레거시 PR 경로가 퇴역하며 no_pr·pr_no_checks·merged_no_report·
draft_merge_loop·fix_dispatch_404 행은 제거됐다. 관측 실패는 observe_failed(T3YT),
CI 실패 위임은 ci_failed(in_progress·TT 경로)가 담당한다.
"""
import pytest

from probe import core as dispatchd
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
    return {"name": name, "enabled": True, "base_url": "http://x", "receive_mode": "hook"}


def _pc(body, ts=OLD, author="probe"):
    return {"id": 1, "author": author, "body": body, "ts": ts}


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("TT_REVIEW_GRACE_MIN", "0")
    monkeypatch.setenv("TT_REVIEW_AGENT", "fallback@t")


# --- 데이터 모듈 ---

def test_규칙표는_필수_키를_가진다():
    rows = failure_rules.rules()
    codes = {r["code"] for r in rows}
    # §4 잔여 행 + reviewer_session_failed(§12 신규) — 레거시 5행은 U4 퇴역으로 제거
    assert {"ci_failed", "observe_failed", "reviewer_session_failed", "evidence_rejected",
            "verify_422", "runner_stall", "budget_exhausted", "needs_decision",
            "lease_conflict_401", "dependency", "marker_mismatch", "stale_base",
            "unknown"} <= codes
    assert not (codes & {"no_pr", "pr_no_checks", "merged_no_report",
                         "draft_merge_loop", "fix_dispatch_404"})
    for r in rows:
        assert r["handling"] in {"R", "R+S", "R+C", "H", "BUG", "C", "미확인"}
        assert r["judgment"] and isinstance(r["wired"], str)


def test_미등록_코드는_unknown_처리를_따른다():
    assert failure_rules.handling_of("never_seen") == "C"
    assert failure_rules.handling_of("budget_exhausted") == "R"


# --- pr_no_checks 배선 ---

def test_checks_관측_불가_None은_판정_대상이_아니다(monkeypatch):
    # 리뷰 R1 — CI 조회 실패(checks=None)는 0건과 다르다: 병합 우회 금지
    monkeypatch.setenv("TT_REVIEW_AGENT", "fallback@t")
    i = _i(reviewer="fallback@t",
           comments=[_pc("review: approve\nPR#9@bbbbbbbb — ok", author="fallback@t")])
    acts = dispatchd.decide(_snap([i], [{"number": 9, "repo": "plainOldCode/think-tank",
                                         "checks": None, "isDraft": False,
                                         "branch": "tt/M4ABCDEF-GH12-x", "title": "",
                                         "head_sha": "b" * 40}], [_agent("a2@t")]))
    assert not [a for a in acts if a["action"] == "merge"]
    assert not [a for a in acts if a["action"] == "review-request"]


def test_행별_처리_구분_골든():
    # 각 행의 code→handling 매핑이 데이터와 일치(행 추가 시 여기도 갱신)
    golden = {"ci_failed": "R+S",
              "reviewer_session_failed": "R", "evidence_rejected": "R+S",
              "verify_422": "R", "runner_stall": "R", "budget_exhausted": "R",
              "needs_decision": "H", "lease_conflict_401": "R",
              "dependency": "H",
              "marker_mismatch": "미확인", "stale_base": "미확인", "unknown": "C"}
    for code, h in golden.items():
        assert failure_rules.handling_of(code) == h, code


def test_checks_0건_게이트_꺼짐은_기존_무응답_유지(monkeypatch):
    monkeypatch.delenv("TT_REVIEW_AGENT", raising=False)
    acts = dispatchd.decide(_snap([_i()], _pr(checks=[]), []))
    assert not [a for a in acts if a["action"] in ("merge", "review-request")]


# --- draft_merge_loop 배선 ---

# --- evidence_rejected 배선 (리뷰 R3: 서버가 DONE_GATE_MSG 시스템 코멘트로 기록함) ---

def test_done_거부_기록_카드는_재제출_요청_노트를_낸다():
    i = _i(state="in_progress",
           comments=[_pc("done 증거 없음 또는 실패/미확정 보고 — 현재 회차의 성공 결과가 필요합니다.",
                         author="tt-server")])
    acts = dispatchd.decide(_snap([i], [], []))
    notes = [a for a in acts if a["action"] == "review-note"
             and (a.get("marker") or "").startswith("[evidence-rejected")]
    assert len(notes) == 1 and notes[0]["marker"] == "[evidence-rejected a1]"
    assert "재제출" in notes[0]["reason"]


def test_done_거부_기록이_없거나_완료된_카드는_무음이다():
    acts = dispatchd.decide(_snap([_i(state="in_progress")], [], []))
    assert not [a for a in acts if a["action"] == "review-note"]
    i = _i(state="done", comments=[_pc("done 증거 없음", author="tt-server")])
    acts = dispatchd.decide(_snap([i], [], []))
    assert not [a for a in acts if a["action"] == "review-note"]


# --- draft × needs-merge 상호작용 (리뷰 R2) ---

# --- 리뷰 2차 R3/R4: 실제 거부 상태 + 전용 마커 집행 + 행별 입력→대응 ---

def test_실제_done_거부_state_review에서도_재제출_요청이_나온다():
    # 서버는 done 거부 시 state=review + DONE_GATE_MSG를 함께 저장 — 그 상태가 대상
    i = _i(state="review",
           comments=[_pc("done 증거 없음 또는 실패/미확정 보고 — 현재 회차의 성공 결과가 필요합니다.",
                         author="tt-server")])
    acts = dispatchd.decide(_snap([i], [], []))
    notes = [a for a in acts if a["action"] == "review-note"
             and (a.get("marker") or "").startswith("[evidence-rejected")]
    assert len(notes) == 1 and notes[0]["marker"] == "[evidence-rejected a1]"


def test_execute는_evidence_전용_마커를_보존하고_dedup한다(monkeypatch):
    import probe.core

    calls = []

    def fake_api(url, path, method="GET", body=None):
        calls.append((method, path, body))
        # 재조회 카드에도 거부 기록이 있어야 요청이 유효(R5: 접수되면 생략)
        return {"comments": [_pc("done 증거 없음 또는 실패/미확정 보고", author="tt-server")],
                "version": 3, "execution_attempt": 1}

    monkeypatch.setattr(probe.core, "api", fake_api)
    act = {"agent": "probe", "issue": "M4ABCDEF-GH12", "action": "review-note",
           "marker": "[evidence-rejected a1]", "attempt": 1,
           "reason": "done 거부(증거 없음/실패)"}
    probe.core.execute("http://x", act)
    posts = [b for m, p, b in calls if p.endswith("/comments") and m == "POST"]
    assert len(posts) == 1 and posts[0]["body"].startswith("[evidence-rejected a1]")
    # 기존 needs-merge 유무와 무관하게 재판정 시 dedup(마커 이미 있으면 무음)
    calls.clear()

    def raced(url, path, method="GET", body=None):
        calls.append((method, path, body))
        return {"comments": [{"author": "probe", "body": "[evidence-rejected a1] done 거부"},
                             {"author": "probe", "body": "[needs-merge a1] PR 없음"},
                             _pc("done 증거 없음 또는 실패/미확정 보고", author="tt-server")],
                "version": 3, "execution_attempt": 1}

    monkeypatch.setattr(probe.core, "api", raced)
    probe.core.execute("http://x", act)
    assert not [b for m, p, b in calls if p.endswith("/comments") and m == "POST"]


def test_미연결_행은_입력이_있어도_무음이다():
    """미연결 4행(runner_stall/verify_422/marker_mismatch/stale_base) — wired="".

    각 행의 관측 입력을 서로 다르게 공급하고, 해당 행 때문에 추가되는
    dispatch/needs-human/merge가 없음을 '전체 액션 집합'으로 검증한다.
    리뷰 R4: needs-human 부재만 보면 예상 밖 work 주입에도 통과하므로 부족.
    """
    # runner_stall — STALL-killed 코멘트(in_progress 기준선: 액션 없음)
    i = _i(state="in_progress",
           comments=[_pc("runner:m2max dispatch#294 STALL-killed exit=9",
                         author="runner@m2max")])
    assert dispatchd.decide(_snap([i], [], [])) == []
    # verify_422 — 카드 기록이 아예 없는 조건(호출자 응답만 존재) = 입력 부재.
    # 기록된 흔적 없이 verify가 실패한 카드 — decide 입력에 나타날 것도 없다.
    assert dispatchd.decide(_snap([_i(state="in_progress")], [], [])) == []
    # marker_mismatch — 규격 밖 마커 코멘트(정식 7종 아님) — 반응 없음
    i = _i(state="in_progress", comments=[_pc("[unknown-garbage-marker xyz] 형식 불일치")])
    assert dispatchd.decide(_snap([i], [], [])) == []
    # stale_base — 리뷰 문맥의 base 지적 코멘트(정식 기록 없음) — 반응 없음
    i = _i(state="in_progress",
           comments=[_pc("rebase 필요 — base가 main 뒤처짐", author="reviewer@t")])
    assert dispatchd.decide(_snap([i], [], [])) == []


def test_행별_입력_to_대응_매핑_문서():
    """배선 행의 입력→대응 검증 위치(행→테스트 연결, 리뷰 R4 3차 — 전체 17행).

               + test_ci_fix_head당_1회_marker면_재위임안함
    reviewer_session_failed: tests/test_reviewer_session_failed.py (15건)
    evidence_rejected: 본 파일 test_실제_done_거부* / test_execute는_evidence_*
    verify_422: 미연결 — 본 파일 test_미연결_행은_입력이_있어도_무음이다 (기록 부재 조건)
    runner_stall: 미연결 — 본 파일 (STALL 입력, 전체 액션 집단 검증)
    budget_exhausted: tests/test_dispatchd.py::test_two_dispatch_tries_becomes_needs_human_not_work
                      + test_attempt_two_becomes_needs_human (사람 통보 기본 대응)
    needs_decision: tests/test_api.py::test_dispatch_progress_blocked_comment_escalates
    lease_conflict_401: tests/test_cross_review.py::test_review_eligible_matrix (review_occupied)
    dependency: tests/test_dispatchd.py::test_release_ready_resume_wins_over_pool
                + test_not_release_ready_is_not_resumed (blocked 재개 조건)
    marker_mismatch: 미확인 — 본 파일 (규격 밖 마커 입력 무음)
    stale_base: 미확인 — 본 파일 (base 지적 입력 무음) + 참고 tests/test_stale_base.py(push 게이트)
    unknown: 본 파일 test_행별_처리_구분_골든 (C 폴백)
    """
    wired = {r["code"]: r["wired"] for r in failure_rules.rules()}
    for code in ("ci_failed", "observe_failed", "reviewer_session_failed",
                 "evidence_rejected", "budget_exhausted", "needs_decision",
                 "lease_conflict_401", "dependency"):
        assert wired[code], code
    for code in ("verify_422", "runner_stall", "marker_mismatch", "stale_base", "unknown"):
        assert not wired[code], code


# --- 리뷰 4차 R5: 성공 보고 접수 후 과거 거부로 재요청하지 않는다 ---

def test_성공_보고_접수된_카드는_과거_거부가_있어도_재요청하지_않는다():
    # 거부 코멘트가 남아 있어도 현재 회차에 유효 보고(reported)가 접수됐으면 해소된 것
    i = _i(state="review", verification_status="reported",
           comments=[_pc("done 증거 없음 또는 실패/미확정 보고", author="tt-server"),
                    _pc("보고 접수 — review에 정지(병합 대기)", author="tt-server")])
    acts = dispatchd.decide(_snap([i], [], []))
    assert not [a for a in acts if a["action"] == "review-note"
                and (a.get("marker") or "").startswith("[evidence-rejected")]


def test_execute도_접수된_보고가_있으면_evidence_요청을_생략한다(monkeypatch):
    import probe.core

    calls = []

    def raced(url, path, method="GET", body=None):
        calls.append((method, path, body))
        return {"comments": [], "version": 3, "execution_attempt": 1,
                "verification_status": "reported"}

    monkeypatch.setattr(probe.core, "api", raced)
    act = {"agent": "probe", "issue": "M4ABCDEF-GH12", "action": "review-note",
           "marker": "[evidence-rejected a1]", "attempt": 1,
           "reason": "done 거부(증거 없음/실패)"}
    probe.core.execute("http://x", act)
    assert not [b for m, p, b in calls if p.endswith("/comments") and m == "POST"]


# --- 리뷰 5차 R5 잔여: 증거 경계 이전 거부 재사용 금지 + 옛 회차 액션 폐기 ---

def test_증거_경계_이전_거부는_새_회차에서_재사용하지_않는다():
    # a1 거부(id=1) → todo→claim(a2): evidence_after_comment_id=1로 리셋.
    # a2에서 done 제출·거부 없음 — 과거 거부로 [evidence-rejected a2] 만들면 안 됨.
    i = _i(state="review", execution_attempt=2, evidence_after_comment_id=1,
           comments=[{"id": 1, "author": "tt-server", "ts": OLD,
                      "body": "done 증거 없음 또는 실패/미확정 보고"}])
    acts = dispatchd.decide(_snap([i], [], []))
    assert not [a for a in acts if (a.get("marker") or "").startswith("[evidence-rejected")]


def test_경계_이후_현재_거부는_여전히_요청한다():
    i = _i(state="review", execution_attempt=2, evidence_after_comment_id=1,
           comments=[{"id": 1, "author": "tt-server", "ts": OLD,
                      "body": "done 증거 없음 또는 실패/미확정 보고"},
                     {"id": 5, "author": "tt-server", "ts": OLD,
                      "body": "done 증거 없음 또는 실패/미확정 보고"}])
    acts = dispatchd.decide(_snap([i], [], []))
    assert [a for a in acts if a.get("marker") == "[evidence-rejected a2]"]


def test_execute는_옛_회차_evidence_액션을_폐기한다(monkeypatch):
    import probe.core

    calls = []

    def raced(url, path, method="GET", body=None):
        calls.append((method, path, body))
        # 카드는 이미 a2로 재수령 — a1 액션은 폐기 대상
        return {"comments": [{"id": 9, "author": "tt-server", "ts": OLD,
                              "body": "done 증거 없음 또는 실패/미확정 보고"}],
                "version": 5, "execution_attempt": 2, "evidence_after_comment_id": 4}

    monkeypatch.setattr(probe.core, "api", raced)
    act = {"agent": "probe", "issue": "M4ABCDEF-GH12", "action": "review-note",
           "marker": "[evidence-rejected a1]", "attempt": 1,
           "reason": "done 거부(증거 없음/실패)"}
    probe.core.execute("http://x", act)
    assert not [b for m, p, b in calls if p.endswith("/comments") and m == "POST"]
    # 현재 회차 액션은 정상 집행
    calls.clear()
    act2 = dict(act, marker="[evidence-rejected a2]", attempt=2)
    probe.core.execute("http://x", act2)
    posts = [b for m, p, b in calls if p.endswith("/comments") and m == "POST"]
    assert len(posts) == 1 and posts[0]["body"].startswith("[evidence-rejected a2]")
