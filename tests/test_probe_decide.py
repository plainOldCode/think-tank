"""decide() 판정표 — probe 정책의 순수 함수 (30KP 설계 docs/core.md).

P4(M5BH91EV-QGGB): probe 명칭으로 정리 — dispatchd 별칭 퇴역.
"""
import pytest

from probe import core
from probe.core import decide

NOW = "2026-09-29T21:00:00+0900"


def issue(iid, state="todo", **kw):
    base = {"id": iid, "state": state, "parent_id": None, "assignee": None,
            "lease_expires": None, "labels": [], "priority": None,
            "execution_attempt": 0, "dispatches": 0, "release_ready": False,
            "waiting_for": None, "title": iid, "brief_verify": "pytest -q"}
    base.update(kw)
    return base


def agent(name="hermes@mini", **kw):
    # RQRT: 등록 에이전트 fixture — 수신 계약 선언이 배정 후보 조건
    base = {"name": name, "enabled": True, "base_url": "http://127.0.0.1:7799",
            "release_hook": True, "receive_mode": "hook"}
    base.update(kw)
    return base


def snap(issues=(), agents=(agent(),), auto=True, prs=()):
    return {"auto": auto, "now": NOW, "agents": list(agents), "issues": list(issues), "prs": list(prs)}


def act(iss, kind, reason):
    return {"agent": "hermes@mini", "issue": iss, "action": kind, "reason": reason}


def test_kill_switch_off_returns_nothing():
    assert decide(snap([issue("A", labels=["auto"])], auto=False)) == []


def test_busy_agent_is_skipped():
    busy = issue("W1", "in_progress", assignee="hermes@mini",
                 lease_expires="2026-09-29T22:00:00+0900")
    assert decide(snap([busy, issue("A", labels=["auto"])])) == []


def test_expired_lease_does_not_block_dispatch():
    stale = issue("W1", "in_progress", assignee="hermes@mini",
                  lease_expires="2026-09-29T20:00:00+0900")
    assert decide(snap([stale, issue("A", labels=["auto"])])) == [
        act("A", "work", "pool")]


def test_continuation_prefers_children_of_last_done():
    done = issue("D1", "done", assignee="hermes@mini", parent_id="P",
                 updated_at="2026-09-29T20:00:00+0900")
    child = issue("C1", labels=["auto"], parent_id="D1")
    other = issue("X1", labels=["auto"])
    assert decide(snap([done, child, other])) == [
        act("C1", "work", "continuation-child")]


def test_continuation_falls_back_to_siblings():
    done = issue("D1", "done", assignee="hermes@mini", parent_id="P",
                 updated_at="2026-09-29T20:00:00+0900")
    sib = issue("S1", labels=["auto"], parent_id="P")
    other = issue("X1", labels=["auto"])
    assert decide(snap([done, sib, other])) == [act("S1", "work", "continuation-sibling")]


def test_release_ready_resume_wins_over_pool():
    dep = issue("DEP", "done")
    blocked = issue("B1", "blocked", labels=["auto"], waiting_for="dependency",
                    release_ready=True, blocked_detail=f"의존: {dep['id']}")
    pool = issue("P1", labels=["auto"])
    assert decide(snap([dep, blocked, pool])) == [act("B1", "resume", "release-ready")]


def test_not_release_ready_is_not_resumed():
    blocked = issue("B1", "blocked", labels=["auto"], waiting_for="dependency",
                    release_ready=False, blocked_detail="의존: M3PG5Q9W-30KP")
    assert decide(snap([blocked])) == []


def test_pool_only_auto_label_priority_order():
    a = issue("P1", labels=["auto"], priority=2)
    b = issue("P2", priority=1)  # no auto label — 사람 범위 통제
    c = issue("P3", labels=["auto"], priority=4)
    assert decide(snap([a, b, c])) == [act("P1", "work", "pool")]


# ---------- 1KFE: [auto-classified] 마커 → 동점자 랭킹 보정 ----------

def _daemon_marked(iid, label, **kw):
    i = issue(iid, labels=["auto"], **kw)
    i["comments"] = [{"author": "tt-embedding-daemon",
                      "body": f"[auto-classified] label={label} gap=0.12 refs=M1,M2"}]
    return i


def test_auto_classified_동점자_사이에서_성적순_보정():
    # 동일 priority 2 — 성적 좋은 라벨(전부 done, att 1) 마커 카드가 먼저 배정
    marked = _daemon_marked("Z9", "security", priority=2)
    plain = issue("A1", labels=["auto"], priority=2)
    priors = [issue("S1", state="done", labels=["security"], execution_attempt=1)]
    assert decide(snap([plain, marked] + priors)) == [act("Z9", "work", "pool")]


def test_auto_classified_나쁜성적_라벨은_뒤로():
    # 전부 needs-human + 회차 3 — 마커 카드가 동점 id순을 깨지 못하고 뒤로
    marked = _daemon_marked("A1", "risk:weird")
    plain = issue("Z9", labels=["auto"])
    priors = [issue(f"W{i}", state="blocked", waiting_for="human",
                    labels=["risk:weird"], execution_attempt=3) for i in range(3)]
    assert decide(snap([plain, marked] + priors)) == [act("Z9", "work", "pool")]


def test_마커없는_카드는_기존_순위_유지():
    # 동점 + 마커 없음 → id순(기존). 선행 카드 존재와 무관
    a = issue("A1", labels=["auto"])
    b = issue("Z9", labels=["auto"])
    priors = [issue("S1", state="done", labels=["security"], execution_attempt=1)]
    assert decide(snap([a, b] + priors)) == [act("A1", "work", "pool")]


def test_미매칭_라벨은_기존_순위_유지():
    # 마커 라벨이 선행 카드에 없음 → 보정 없음(라벨 의미 선언 없이 동작)
    marked = _daemon_marked("Z9", "never-seen")
    plain = issue("A1", labels=["auto"])
    assert decide(snap([plain, marked])) == [act("A1", "work", "pool")]


def test_auto_classified_라벨_필드_불변과_로그(capsys):
    marked = _daemon_marked("Z9", "security")
    priors = [issue("S1", state="done", labels=["security"], execution_attempt=1)]
    s = snap([marked] + priors)
    decide(s)
    after = {x["id"]: list(x["labels"]) for x in s["issues"]}
    assert after["Z9"] == ["auto"]  # 라벨 필드 불변 — 데몬 참표 오염 방지
    assert after["S1"] == ["security"]
    out = capsys.readouterr().out
    assert "[auto-classified]" in out and "Z9" in out  # 로그 1줄 근거


def test_two_dispatch_tries_becomes_needs_human_not_work():
    t = issue("T1", labels=["auto"], dispatches=2)
    assert decide(snap([t])) == [act("T1", "needs-human", "budget:dispatch-tries>=2")]


def test_claims_two_becomes_needs_human():
    t = issue("T1", labels=["auto"], claim_count=2)
    assert decide(snap([t])) == [act("T1", "needs-human", "budget:claims>=2")]


def test_body_edit_attempt_does_not_block_auto_dispatch():
    """M5BNMHB7-RBQE — 본문 수정만으로 오른 attempt(claim 0)는 자동 배정을 막지 않는다."""
    t = issue("T1", labels=["auto"], execution_attempt=2)
    assert decide(snap([t])) == [act("T1", "work", "pool")]


def test_disabled_or_hookless_agent_is_skipped():
    done = issue("A", labels=["auto"])
    assert decide(snap([done], [agent(enabled=False)])) == []
    assert decide(snap([done], [agent(base_url="")])) == []


def test_no_double_claim_across_agents():
    p1 = issue("P1", labels=["auto"], priority=1)
    p2 = issue("P2", labels=["auto"], priority=2)
    a1 = agent("a1@mini")
    a2 = agent("a2@mini")
    out = decide(snap([p1, p2], [a1, a2]))
    assert len(out) == 2
    assert {o["issue"] for o in out} == {"P1", "P2"}
    assert len({o["agent"] for o in out}) == 2


def test_done_done_without_parent_falls_to_pool():
    done = issue("D1", "done", assignee="hermes@mini",
                 updated_at="2026-09-29T20:00:00+0900")
    pool = issue("P1", labels=["auto"])
    assert decide(snap([done, pool])) == [act("P1", "work", "pool")]


def test_ci_passed_rules():
    ok = {"number": 3, "head_sha": "abc", "checks": [{"name": "ci", "state": "SUCCESS"}]}
    assert core.ci_passed(ok) is True
    assert core.ci_passed(dict(ok, checks=[])) is False
    assert core.ci_passed(dict(ok, checks=[{"name": "ci", "state": "FAILURE"}])) is False
    assert core.ci_passed(dict(ok, checks=[{"name": "ci", "state": "SUCCESS"},
                                                {"name": "smoke", "state": "PENDING"}])) is False


GREEN_PR = {"number": 3, "head_sha": "abc", "branch": "tt/M3PXXXXX-9ABC-ci-probe",
            "checks": [{"name": "ci", "state": "SUCCESS"}]}


def test_decide_no_merge_without_ci():
    iss = issue("M3PXXXXX-9ABC", state="in_progress", assignee="codex",
                lease_expires="2026-09-29T23:00:00+0900", execution_attempt=1)
    iss["work_contract"] = {"version": "tt-tdd-v2:x"}
    cold = dict(GREEN_PR, checks=[])
    acts = decide(snap([iss], prs=[cold]))
    assert not [a for a in acts if a["action"] == "merge"]


def test_decide_no_merge_without_card_or_reportless_ok():
    # 카드 없음(PR이 고아) → merge 없음
    acts = decide(snap([issue("A", labels=["auto"])], prs=[GREEN_PR]))
    assert not [a for a in acts if a["action"] == "merge"]
    # attempt=0(수령 전) → merge 없음
    iss = issue("M3PXXXXX-9ABC", state="in_progress", execution_attempt=0)
    iss["work_contract"] = {"version": "tt-tdd-v2:x"}
    acts = decide(snap([iss], prs=[GREEN_PR]))
    assert not [a for a in acts if a["action"] == "merge"]


def test_card_repo_parsing():
    assert core.card_repo({"body": "참조 repo: plainOldCode/armour-service-ops 링크"}) == "plainOldCode/armour-service-ops"
    assert core.card_repo({"body": "repo: a/b", "title": "x"}) == "a/b"
    assert core.card_repo({"body": "no marker", "title": "t"}) is None


# --- M4NCNNRW-PXCJ: 실행 불가 에이전트(어댑터·리드온리) 배정 제외 ---

def test_auto_풀은_실행불가_에이전트에_일을_주지_않는다():
    ka = agent("kanban-adapter")
    codex = agent("codex")
    i = issue("A", labels=["auto"])
    acts = decide(snap([i], agents=[ka, codex]))
    assert acts and acts[0]["agent"] == "codex" and acts[0]["action"] == "work"


def test_실행불가만_유휴면_work_스폰_없다():
    ka = agent("kanban-adapter")
    assert decide(snap([issue("A", labels=["auto"])], agents=[ka])) == []


def test_release_ready_resume도_실행불가_에이전트_피한다():
    dep = issue("DEP", "done")
    blocked = issue("B1", "blocked", labels=["auto"], waiting_for="dependency",
                    release_ready=True, blocked_detail="의존: DEP")
    ka = agent("kanban-adapter")
    codex = agent("codex")
    acts = decide(snap([dep, blocked], agents=[ka, codex]))
    assert acts and acts[0]["action"] == "resume" and acts[0]["agent"] == "codex"
