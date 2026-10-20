"""U3(M5A4HYHM-4WTN) — TT 경로 decide golden.

decide golden 9종은 레거시 PR 경로(work·review-note·merge·review-fix)만 다룬다 —
실제 운영 주 경로(TT 제출물 라이프사이클)의 액션 출력을 여기서 고정한다.
U4(레거시 제거)와 tt_lifecycle.execute 분할의 안전망이다.

- 스냅샷: tests/golden/tt_cases/*.json (입력, 기록용)
- 기대 출력: tests/golden/tt_expected/<이름>.json
- 재생성: TT_REGEN_GOLDEN=1 python -m pytest tests/test_tt_golden.py -q
  (의도된 판정 변경 시에만 — 리뷰 근거와 함께)
"""
import copy
import json
import os
import pathlib

import pytest
from probe.core import decide
from review_subject import subject as _subject

GOLDEN = pathlib.Path(__file__).parent / "golden"
CASES = GOLDEN / "tt_cases"
EXPECTED = GOLDEN / "tt_expected"

NOW = "2026-10-20T02:00:00+0900"
OLD = "2026-10-20T00:00:00+0900"

REPORT = {
    "contract_version": "tt-tdd-v2.2:018f6c6fc87d07e6c7a9ddf07e3986aff5b828dcb4009719bc1695b8f175f96d",
    "attempt": 1, "method": "planned", "result": "passed",
    "design": {"criteria": "c", "verification": "v"},
    "implementation": {"summary": "s", "commands": "cmd"},
    "verification": {"commands": "pytest -q", "evidence": "1 passed",
                     "verification_level": "unit-test-verified"},
}
REPORT_PR = {**REPORT, "delivery": {"mode": "pr", "provider": "github",
                                    "repo": "plainOldCode/think-tank",
                                    "number": 9, "head_sha": "b" * 40,
                                    "base": "main"}}


def agent(name, **kw):
    base = {"name": name, "enabled": True, "base_url": "http://x/hook",
            "receive_mode": "hook"}
    base.update(kw)
    return base


def tt_issue(**kw):
    base = {"id": "M4TTTT-GH01", "state": "review", "parent_id": None,
            "assignee": "worker", "lease_expires": None, "labels": [],
            "priority": None, "execution_attempt": 1, "dispatches": 1,
            "release_ready": False, "waiting_for": None, "title": "tt card",
            "brief_verify": "pytest -q", "reviewer": "", "archived": 0,
            "version": 3, "comments": [], "updated_at": OLD, "busy_agents": [],
            "work_contract": {"version": REPORT["contract_version"]},
            "completion_report": REPORT}
    base.update(kw)
    return base


TOKEN = _subject(tt_issue())
TOKEN_PR = _subject(tt_issue(completion_report=REPORT_PR))
VERDICT_OK = f"review: approve\n{TOKEN}\n근거: 독립 검증 통과"
VERDICT_OK_PR = f"review: approve\n{TOKEN_PR}\n근거: 독립 검증 통과"
VERDICT_RC = f"review: request-changes\n{TOKEN}\n테스트 회귀 — 수정 후 재제출"
KILL_TS = "2026-10-20T01:48:00+0900"


def pc(body, author="probe", ts=OLD):
    return {"id": 1, "author": author, "body": body, "ts": ts}


def pr(number=9, sha="b" * 40):
    return {"number": number, "repo": "plainOldCode/think-tank",
            "checks": [{"state": "SUCCESS"}], "isDraft": False,
            "branch": f"tt/M4TTTT-GH01-x", "title": "", "head_sha": sha}


def cases():
    """TT 경로 주요 분기 — 신규 리뷰, STALL 재발령(ME5W), 반려 fix, verify(report),
    deliver(PR 영수증), delivery-hold, review-stalled 통지."""
    stall = tt_issue(reviewer="codex",
                     lease_expires="2026-10-20T03:00:00+0900",
                     comments=[pc(f"[tt-review {TOKEN}] reviewer=codex"),
                               pc("runner:m2max dispatch#601 STALL-killed — 진행 신호 없음 "
                                  "session=tt-codex-601", author="runner@m2max", ts=KILL_TS)])
    fix = tt_issue(reviewer="codex",
                   comments=[pc(VERDICT_RC, author="codex", ts=NOW)])
    verify = tt_issue(reviewer="codex",
                      comments=[pc(VERDICT_OK, author="codex", ts=NOW)])
    deliver = tt_issue(reviewer="codex", completion_report=REPORT_PR,
                       comments=[pc(VERDICT_OK_PR, author="codex", ts=NOW)])
    hold = tt_issue(reviewer="codex", busy_agents=["worker"],
                    completion_report=REPORT_PR,
                    comments=[pc(VERDICT_OK_PR, author="codex", ts=NOW)])
    stalled = tt_issue(comments=[pc(f"[tt-review {TOKEN}] reviewer=codex"),
                                 pc(f"[tt-review {TOKEN}] reviewer=claude", ts=OLD)])
    return {
        "tt_review_new": {"auto": True, "now": NOW, "issues": [tt_issue()], "prs": [],
                          "agents": [agent("worker"), agent("codex"), agent("claude")]},
        "tt_review_stall_redispatch": {
            "auto": True, "now": NOW, "issues": [stall], "prs": [],
            "agents": [agent("worker"), agent("codex"), agent("claude")]},
        "tt_review_fix": {"auto": True, "now": NOW, "issues": [fix], "prs": [],
                          "agents": [agent("worker"), agent("codex"), agent("claude")]},
        "tt_verify_report": {"auto": True, "now": NOW, "issues": [verify], "prs": [],
                             "agents": [agent("worker"), agent("codex"), agent("claude")]},
        "tt_deliver_pr": {
            "auto": True, "now": NOW, "issues": [deliver], "prs": [pr()],
            "agents": [agent("worker"), agent("codex"), agent("claude")],
            "delivery_context": {"M4TTTT-GH01": {
                **REPORT_PR["delivery"], "state": "OPEN", "isDraft": False,
                "base": "main", "merge_commit": None, "merged_at": None,
                "checks": [{"name": "tests", "state": "SUCCESS"}]}}},
        "tt_delivery_hold": {
            "auto": True, "now": NOW, "issues": [hold], "prs": [pr()],
            "agents": [agent("worker"), agent("codex"), agent("claude")],
            "delivery_context": {"M4TTTT-GH01": {
                **REPORT_PR["delivery"], "state": "OPEN", "isDraft": False,
                "base": "main", "merge_commit": None, "merged_at": None,
                "checks": [{"name": "tests", "state": "FAILURE"}]}}},
        "tt_review_stalled": {"auto": True, "now": NOW, "issues": [stalled], "prs": [],
                              "agents": [agent("worker"), agent("codex"), agent("claude")]},
    }


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.delenv("TT_STALL_REDISPATCH_GRACE_MIN", raising=False)
    monkeypatch.delenv("TT_REVIEW_MAP", raising=False)


def test_golden_tt_출력은_분할_전후_동일하다():
    EXPECTED.mkdir(parents=True, exist_ok=True)
    CASES.mkdir(parents=True, exist_ok=True)
    regen = os.environ.get("TT_REGEN_GOLDEN") == "1"
    for name, snapdata in cases().items():
        (CASES / f"{name}.json").write_text(json.dumps(snapdata, ensure_ascii=False, indent=1))
        got = decide(copy.deepcopy(snapdata))
        f = EXPECTED / f"{name}.json"
        if regen or not f.exists():
            f.write_text(json.dumps(got, ensure_ascii=False, indent=1, sort_keys=True))
            continue
        want = json.loads(f.read_text())
        assert got == want, (f"golden 불일치: {name}\n"
                             f"기대: {json.dumps(want, ensure_ascii=False)[:400]}\n"
                             f"실제: {json.dumps(got, ensure_ascii=False)[:400]}")
