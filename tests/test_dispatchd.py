"""decide() 판정표 — dispatchd 정책의 순수 함수 (30KP 설계 docs/dispatchd.md)."""
import pytest

from dispatchd import decide
import dispatchd

NOW = "2026-09-29T21:00:00+0900"


def issue(iid, state="todo", **kw):
    base = {"id": iid, "state": state, "parent_id": None, "assignee": None,
            "lease_expires": None, "labels": [], "priority": None,
            "execution_attempt": 0, "dispatches": 0, "release_ready": False,
            "waiting_for": None, "title": iid}
    base.update(kw)
    return base


def agent(name="hermes@mini", **kw):
    base = {"name": name, "enabled": True, "base_url": "http://127.0.0.1:7799",
            "release_hook": True}
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


def test_two_dispatch_tries_becomes_needs_human_not_work():
    t = issue("T1", labels=["auto"], dispatches=2)
    assert decide(snap([t])) == [act("T1", "needs-human", "dispatch-tries>=2")]


def test_attempt_two_becomes_needs_human():
    t = issue("T1", labels=["auto"], execution_attempt=2)
    assert decide(snap([t])) == [act("T1", "needs-human", "attempt>=2")]


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
    assert dispatchd.ci_passed(ok) is True
    assert dispatchd.ci_passed(dict(ok, checks=[])) is False
    assert dispatchd.ci_passed(dict(ok, checks=[{"name": "ci", "state": "FAILURE"}])) is False
    assert dispatchd.ci_passed(dict(ok, checks=[{"name": "ci", "state": "SUCCESS"},
                                                {"name": "smoke", "state": "PENDING"}])) is False


GREEN_PR = {"number": 3, "head_sha": "abc", "branch": "tt/M3PXXXXX-9ABC-ci-probe",
            "checks": [{"name": "ci", "state": "SUCCESS"}]}


def test_decide_merges_green_pr_with_card():
    from dispatchd import ci_passed
    iss = issue("M3PXXXXX-9ABC", state="in_progress", assignee="codex",
                lease_expires="2026-09-29T23:00:00+0900", execution_attempt=1)
    iss["work_contract"] = {"version": "tt-tdd-v2:x"}
    acts = decide(snap([iss], prs=[GREEN_PR]))
    assert acts == [{"agent": "probe", "issue": "M3PXXXXX-9ABC", "action": "merge", "pr": 3,
                     "reason": "CI green + 카드 계약/수령 검증 — gh pr merge"}]


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
