#!/usr/bin/env python3
"""Run 상태기계 — 명시적 전이표 (TT 개선#1 M4DEDPK2-0VSF 요구 1).

runner 장부(runs.json — 단일 파일 유지)의 상태 전이를 이 모듈이 유일하게 결정한다.
tt-runner.py는 상태를 문자열로 직접 쓰지 않고 transition()을 경유한다.

상태:
- queued      접수됨, 실행 대기
- running     tmux 세션 실행 중
- stalled     실행 중이나 진행 신호 없음 (STALL 통지 후, 복구 가능)
- finalizing  완료 보고 소유권 획득, 종료 보고 진행 중 (회차 소유 CAS 성공 후)
- held        파괴적 패턴 게이트로 보류 (승인 대기)
- done / failed / blocked / cancelled  종단 — 전이 불가(재실행은 새 회차 엔트리)
"""

STATES = frozenset({
    "queued", "running", "stalled", "finalizing", "held",
    "done", "failed", "blocked", "cancelled",
})

TERMINAL = frozenset({"done", "failed", "blocked", "cancelled"})

# release_issue(release 명령)가 중단 대상으로 삼는 활성 상태
ACTIVE = frozenset({"queued", "running", "stalled", "finalizing", "held"})

# 완료/실패 보고(update_run_if_round)가 전환을 허용하는 출발 상태 — finalizing은
# 보고 소유 CAS(claim_report)가 통과한 상태, running/stalled는 구형 장부 호환.
REPORTABLE = frozenset({"running", "stalled", "finalizing"})

# stalled 판정과 복구가 관여하는 상태
STALLABLE = frozenset({"running", "stalled"})

TRANSITIONS = {
    "queued": {"running", "held", "failed", "cancelled"},
    # running/stalled → done|failed|blocked 직행은 구형 장부(레거시 finalize) 호환 경로.
    # 신규 경로는 보고 소유 CAS(claim_report)의 finalizing을 통과한다 — 회차 가드는
    # update_run_if_round의 _observes_round가 담당하므로 직행 허용이 A→B→C→B 방어를
    # 약화시키지 않는다.
    "running": {"stalled", "finalizing", "done", "failed", "blocked", "cancelled"},
    "stalled": {"running", "finalizing", "done", "failed", "blocked", "cancelled"},
    "finalizing": {"done", "failed", "blocked", "cancelled"},
    "held": {"queued", "cancelled"},
    "done": set(),
    "failed": set(),
    "blocked": set(),
    "cancelled": set(),
}


class IllegalTransition(ValueError):
    """전이표에 없는 상태 전이 시도."""


def can_transition(cur, new):
    """cur → new 전이가 전이표에 있는지. 알 수 없는 현재 상태는 전이 불가(보수적)."""
    if cur not in STATES or new not in STATES:
        return False
    return new in TRANSITIONS[cur]


def transition(ent, new, **fields):
    """엔트리 dict에 상태 전이를 적용하고 fields를 병합한다.

    불법 전이면 IllegalTransition을 던지고 엔트리를 건드리지 않는다(원자성 —
    호출자는 lock 안에서 사용, 예외 시 save 생략).
    """
    cur = ent.get("status")
    if not can_transition(cur, new):
        raise IllegalTransition("%s → %s" % (cur, new))
    ent["status"] = new
    if fields:
        ent.update(fields)
    return ent
