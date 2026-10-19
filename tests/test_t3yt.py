"""T3YT(M58SPT6D-T3YT) — PR 조회 실패 degraded 표식 + observe_failed 사유.

'조회 실패'와 'PR 없음'의 분리: repo 단위 pr list 실패가 no_pr로 오탐되지 않게 한다.
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server", "probe"))

import pytest

from probe import core
import probe.providers as providers


OLD = "2026-10-01T09:00:00+0900"
NOW = "2026-10-19T09:00:00+0900"
REPO = "plainOldCode/think-tank"


def issue(iid, state="review", **kw):
    base = {"id": iid, "state": state, "parent_id": None, "assignee": "hermes@mini",
            "lease_expires": None, "labels": [], "priority": None,
            "execution_attempt": 1, "dispatches": 1, "release_ready": False,
            "waiting_for": None, "title": iid, "brief_verify": "pytest -q",
            "reviewer": "fallback@t", "archived": 0, "version": 3, "comments": [],
            "updated_at": OLD,
            "work_contract": {"version": "tt-tdd-v2.2:aa"},
            "body": f"repo: {REPO}"}
    base.update(kw)
    return base


def agent(name="hermes@mini", **kw):
    base = {"name": name, "enabled": True, "base_url": "http://127.0.0.1:7799",
            "release_hook": True, "receive_mode": "hook"}
    base.update(kw)
    return base


def pc(body, author="probe", ts=OLD):
    return {"id": 1, "author": author, "body": body, "ts": ts}


def snap(issues=(), agents=(agent(),), prs=(), **extra):
    s = {"auto": True, "now": NOW, "agents": list(agents),
         "issues": list(issues), "prs": list(prs)}
    s.update(extra)
    return s


def probe_bodies(i):
    return [c["body"] for c in i["comments"] if c.get("author") == "probe"]


# ---------- providers: repo 단위 관측 기록 ----------

def test_collect_github_records_failed_repo():
    def query(*args):
        repo = args[args.index("--repo") + 1]
        if repo == "o/broken":
            raise RuntimeError("gh auth expired")
        if args[0] == "pr":
            return []
        return None

    prs, observed = providers.collect_github(["o/broken", "o/fine"], query)
    assert prs == []
    assert observed == {"o/broken": "failed", "o/fine": "ok"}


def test_collect_github_ok_keeps_checks_semantics():
    def query(*args):
        if args[0] == "pr" and args[1] == "checks":
            raise RuntimeError("checks api down")  # 관측 실패 ≠ 0건 — None 유지
        if args[0] == "pr":
            return [{"number": 9, "headRefName": "x", "headRefOid": "b" * 40,
                     "baseRefName": "main", "title": "", "isDraft": False, "labels": []}]
        return None

    prs, observed = providers.collect_github(["o/fine"], query)
    assert observed == {"o/fine": "ok"}
    assert prs[0]["checks"] is None  # 기존 의미론: checks 실패는 0건이 아니라 미확인


def test_collect_provider_none_records_no_failure():
    os.environ["TT_PR_PROVIDER"] = "none"
    try:
        assert providers.collect(["o/x"], None) == ([], {})
    finally:
        del os.environ["TT_PR_PROVIDER"]


# ---------- collect_prs → snap 승격 ----------

def test_collect_prs_carries_observation_and_snap_promotes(monkeypatch):
    monkeypatch.setattr(providers, "collect",
                        lambda repos, q: ([], {"o/a": "failed", "o/b": "ok"}))
    prs = core.collect_prs(["o/a", "o/b"])
    assert prs == []
    snap = {}
    core._promote_pr_observation(snap)
    assert snap["pr_observed"] == {"o/a": "failed", "o/b": "ok"}
    assert snap["degraded"] == ["pr_list"]
    assert core.collect_prs.observed == {}  # 소비 후 초기화 — 사이클 오염 방지


def test_snap_promotion_all_ok_not_degraded(monkeypatch):
    monkeypatch.setattr(providers, "collect", lambda repos, q: ([], {"o/a": "ok"}))
    core.collect_prs(["o/a"])
    snap = {}
    core._promote_pr_observation(snap)
    assert snap["degraded"] == []


# ---------- decide: observe_failed 분기 (RED→GREEN 핵심) ----------

def test_failed_repo_not_judged_no_pr():
    """RED: 관측 실패 repo의 카드가 no_pr(needs-merge)로 판정되던 회귀."""
    i = issue("M4ABCDEF-GH12")
    s = snap([i], prs=[], pr_observed={REPO: "failed"}, degraded=["pr_list"])
    acts = core.decide(s)
    notes = [a for a in acts if a.get("issue") == i["id"]]
    assert not any("needs-merge" in (a.get("marker") or "") for a in notes), \
        "관측 실패 repo는 'PR 없음' 판정 금지"
    om = [a for a in notes if "observe-failed" in (a.get("marker") or "")]
    assert om and REPO in om[0]["reason"]


def test_ok_repo_still_judged_no_pr():
    """관측 성공 repo의 카드는 기존 no_pr 판정 유지(동작 불변)."""
    i = issue("M4ABCDEF-GH12")
    s = snap([i], prs=[], pr_observed={REPO: "ok"}, degraded=[])
    acts = core.decide(s)
    assert any(a.get("action") == "review-note" and "사람 판단 대기" in (a.get("reason") or "")
               for a in acts if a.get("issue") == i["id"])
    assert not any("observe-failed" in (a.get("marker") or "") for a in acts)


def test_unknown_repo_card_degraded_treated_failed():
    """후보 repo를 전혀 모르는 카드 + 전체 degraded → 보수적으로 관측 실패."""
    i = issue("M4ABCDEF-GH12", body="no repo marker")
    s = snap([i], prs=[], pr_observed={"o/other": "failed"}, degraded=["pr_list"])
    acts = core.decide(s)
    assert any("observe-failed" in (a.get("marker") or "") for a in acts
               if a.get("issue") == i["id"])


def test_no_observation_data_keeps_old_behavior():
    """관측 데이터 없는 스냅(구버전/스텁) — 기존 판정 그대로(golden 호환)."""
    i = issue("M4ABCDEF-GH12")
    s = snap([i], prs=[])
    acts = core.decide(s)
    assert any(a.get("action") == "review-note" and "사람 판단 대기" in (a.get("reason") or "")
               for a in acts if a.get("issue") == i["id"])


def test_observe_failed_dedup_and_escalation():
    """마커 회차당 1회 dedup + N회 반복 시 §6 사람 통보."""
    i = issue("M4ABCDEF-GH12",
              comments=[pc("[observe-failed a1] probe", author="probe")])
    s = snap([i], prs=[], pr_observed={REPO: "failed"}, degraded=["pr_list"])
    acts = core.decide(s)
    assert not any("observe-failed" in (a.get("marker") or "") for a in acts
                   if a.get("issue") == i["id"]), "동일 회차 마커 재발행 금지"

    i2 = issue("M4ABCDEF-GH12",
               comments=[pc("[observe-failed a1]", author="probe"),
                         pc("[observe-failed a2]", author="probe"),
                         pc("[observe-failed a3]", author="probe")])
    s2 = snap([i2], prs=[], pr_observed={REPO: "failed"}, degraded=["pr_list"])
    acts2 = core.decide(s2)
    hits = [a for a in acts2 if a.get("issue") == i2["id"]]
    assert any(a.get("action") == "needs-human" for a in hits), "반복 → 사람 통보"
    assert not any("observe-failed" in (a.get("marker") or "") for a in hits)


# ---------- 규칙표 ----------

def test_failure_rules_row():
    import failure_rules
    r = failure_rules.get("observe_failed")
    assert r and r["handling"] == "R"
    assert "재시도" in r["default"]
