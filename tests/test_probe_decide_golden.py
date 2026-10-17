"""AK87-1 전제 — decide() golden 고정 테스트.

probe.core.decide는 750줄 거대 함수다(부모 M54XHS0R-AK87). 함수 분할 시
판정 결과가 변하면 안 되므로, 대표 스냅샷 세트의 '전체 액션 출력'을 현재
동작으로 저장해 분할 전후 동일성을 보증한다. 판정표 의미 자체는
test_dispatchd.py·test_failure_rules.py가 이미 잠그므로, 여기는 분할
리팩터용 전체 출력 스냅샷이다.

- 스냅샷: tests/golden/decide_cases/*.json (입력, 기록용)
- 기대 출력: tests/golden/decide_expected/<이름>.json
- 재생성: TT_REGEN_GOLDEN=1 python -m pytest tests/test_probe_decide_golden.py -q
  (의도된 판정 변경 시에만 — 리뷰 근거와 함께)
"""
import copy
import json
import os
import pathlib

import pytest
from dispatchd import decide

GOLDEN = pathlib.Path(__file__).parent / "golden"
CASES = GOLDEN / "decide_cases"
EXPECTED = GOLDEN / "decide_expected"

NOW = "2026-09-29T21:00:00+0900"
OLD = "2026-09-29T20:00:00+0900"


def issue(iid, state="todo", **kw):
    base = {"id": iid, "state": state, "parent_id": None, "assignee": None,
            "lease_expires": None, "labels": [], "priority": None,
            "execution_attempt": 0, "dispatches": 0, "release_ready": False,
            "waiting_for": None, "title": iid, "brief_verify": "pytest -q",
            "reviewer": "", "archived": 0, "version": 3, "comments": [],
            "updated_at": OLD}
    base.update(kw)
    return base


def agent(name="hermes@mini", **kw):
    base = {"name": name, "enabled": True, "base_url": "http://127.0.0.1:7799",
            "release_hook": True, "receive_mode": "hook"}
    base.update(kw)
    return base


def pr(number=9, sha="b" * 40, checks="ok", draft=False, iid="M4ABCDEF-GH12"):
    return {"number": number, "repo": "plainOldCode/think-tank",
            "checks": [{"state": "SUCCESS"}] if checks == "ok" else checks,
            "isDraft": draft, "branch": f"tt/{iid}-x", "title": "",
            "head_sha": sha}


def pc(body, author="probe", ts=OLD):
    return {"id": 1, "author": author, "body": body, "ts": ts}


def snap(issues=(), agents=(agent(),), prs=()):
    return {"auto": True, "now": NOW, "agents": list(agents),
            "issues": list(issues), "prs": list(prs)}


VERDICT = "review: approve\nPR#9@bbbbbbbb — ok"
RC = "review: request-changes\nPR#9@bbbbbbbb\n테스트 회귀 — 수정 후 재제출"


def cases():
    """분할 대상 decide의 주요 분기 — 각각 실제 액션 또는 의도적 무음."""
    return {
        # 신규 배정
        "assign_todo": snap([issue("A", labels=["auto"])]),
        # 완료 부모의 하위 이어하기
        "continuation_child": snap([issue("D1", "done", assignee="hermes@mini",
                                          parent_id="P", updated_at=OLD),
                                    issue("C1", labels=["auto"], parent_id="D1"),
                                    issue("X1", labels=["auto"])]),
        # 만료 lease는 재배정 가능
        "expired_lease": snap([issue("W1", "in_progress", assignee="hermes@mini",
                                     lease_expires=OLD),
                               issue("A", labels=["auto"])]),
        # checks 0건 + 유효 승인 판정 → 병합 후보
        "merge_checks0_verdict": snap(
            [issue("M4ABCDEF-GH12", "review", reviewer="fallback@t", execution_attempt=1,
                   dispatches=1, work_contract={"version": "v2"},
                   comments=[pc(VERDICT, author="fallback@t")])],
            prs=[pr(checks=[])]),
        # RC 판정 → review-fix 재위임
        "review_fix_rc": snap(
            [issue("M4ABCDEF-GH12", "review", reviewer="fallback@t", assignee="hermes@mini",
                   execution_attempt=1, dispatches=1,
                   work_contract={"version": "v2"},
                   comments=[pc(RC, author="fallback@t")])],
            prs=[pr()]),
        # 유효 lease — 건드리지 않음(무음 고정)
        "in_progress_lease": snap([issue("W1", "in_progress", assignee="hermes@mini",
                                         lease_expires=NOW)]),
        # 종단 상태 — 무음
        "done_static": snap([issue("D1", "done")]),
        # draft PR — ready 요청 노트
        "draft_pr": snap([issue("M4ABCDEF-GH12", "review", execution_attempt=1, dispatches=1,
                                work_contract={"version": "v2"})],
                         prs=[pr(draft=True)]),
        # 별칭 보유 에이전트 — 배정 후보 정상 해석
        "alias_agent": snap([issue("A", labels=["auto"])],
                            agents=[agent(aliases=["skshim@tp-13"])]),
    }


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("TT_REVIEW_GRACE_MIN", "0")
    monkeypatch.setenv("TT_REVIEW_AGENT", "fallback@t")
    monkeypatch.delenv("TT_DRAFT_STALL_H", raising=False)


def test_golden_decide_출력은_분할_전후_동일하다():
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
