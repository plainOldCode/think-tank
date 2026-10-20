"""R2 반복 사건 억제 (TT M4K87YR0-HN7E) — §6 (사유코드,범위) 카운터.

키=(사유코드, 범위) 반복 N회 도달 시 사람 통보 1회 + 이후 자동 대응 스킵.
해제는 사람 전용(코멘트) — probe 자동 초기화 금지. budget_exhausted 회피 없음.
"""
import re

import pytest

from probe import core
import failure_rules

NOW = "2026-10-09T10:00:00+0900"
OLD = "2026-10-09T09:00:00+0900"
SHA8 = "abcde123"
KEY_CI = f"(ci_failed, #{9}/{SHA8})"


def _i(**kw):
    base = {"id": "M4ABCDEF-GH12", "state": "review", "archived": 0, "version": 3,
            "assignee": "a1@t", "reviewer": "", "lease_by": "", "lease_expires": None,
            "labels": [], "dispatches": 1, "execution_attempt": 1,
            "work_contract": {"version": "v2"}, "comments": [],
            "updated_at": "2026-10-09T09:00:00+0900", "title": "M4ABCDEF-GH12"}
    base.update(kw)
    return base


def _snap(issues, prs=(), agents=()):
    return {"auto": True, "now": NOW, "agents": list(agents),
            "issues": list(issues), "prs": list(prs)}


def _pr(number=9, sha="abcde1234567890", checks=None, draft=False):
    return {"number": number, "repo": "plainOldCode/think-tank",
            "checks": checks if checks is not None else [{"state": "SUCCESS"}],
            "isDraft": draft, "branch": "tt/M4ABCDEF-GH12-x", "title": "",
            "head_sha": sha}


def _pc(body, ts=OLD, author="probe", cid=1):
    return {"id": cid, "author": author, "body": body, "ts": ts}


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.delenv("TT_SUPPRESS_N", raising=False)


# --- ci_failed: (코드, #pr/sha8) 카운터 ---

def _ci_card(n, att=1, **kw):
    extra = kw.pop("comments", [])
    return _i(execution_attempt=att,
              comments=[_pc(f"[ci-fix a{k} #9/{SHA8}]", cid=k) for k in range(1, n + 1)] + extra)


def test_억제_중인_키는_재통보하지_않는다():
    i = _ci_card(3, comments=[_pc(f"[escalated {KEY_CI}] 반복 사건 — 사람 확인 대기",
                                  author="dispatchd", cid=9)])
    acts = core.decide(_snap([i], [_pr(checks=[{"state": "FAILURE"}])], []))
    assert not [a for a in acts if a["action"] in ("ci-fix", "needs-human")]


def test_probe가_해제_마커를_남겨도_초기화되지_않는다():
    rel = _pc(f"[unblocked {KEY_CI}] 자가 해제 시도", author="probe", cid=9)
    i = _ci_card(3, att=4, comments=[_pc(f"[escalated {KEY_CI}] 통보", author="dispatchd", cid=8), rel])
    acts = core.decide(_snap([i], [_pr(checks=[{"state": "FAILURE"}])], []))
    assert not [a for a in acts if a["action"] == "ci-fix"]


# --- draft_merge_loop / no_pr: 카드·PR 범위 카운터 ---

def test_억제는_budget_에스컬레이션을_막지_않는다(monkeypatch):
    # §6 제약 — needs-human 완화 금지: 다른 키가 억제돼도 ③ 예산 소진 통보는 유지
    monkeypatch.setenv("TT_MAX_DISPATCH", "1")
    t = {"id": "M4BUDGET-TEST", "state": "todo", "parent_id": None, "assignee": None,
         "lease_expires": None, "labels": ["auto"], "priority": None,
         "execution_attempt": 0, "dispatches": 2, "release_ready": False,
         "waiting_for": None, "title": "M4BUDGET-TEST", "comments": [],
         "archived": 0, "reviewer": "", "lease_by": "", "lease_by_exp": None,
         "updated_at": "2026-10-09T09:00:00+0900"}
    i = _ci_card(3, comments=[_pc(f"[escalated {KEY_CI}]", author="dispatchd", cid=9)])
    acts = core.decide(_snap([t, i], [], []))
    nh = [a for a in acts if a["action"] == "needs-human"]
    assert any("budget" in (a.get("reason") or "") for a in nh)


