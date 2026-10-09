"""TT 개선#3b — busy/eligible 단일 정책 모듈.

기록 카드 상태·작업 lease·리뷰 점유·runner 실행의 네 차원을 한곳에서 판정한다.
probe(자동 배정)·서버 /pull·러너 수령이 같은 reason code를 공유한다.

reason code는 안정(stable) 문자열 — 로그·코멘트 파싱 전제라 형식을 바꾸지 않는다.
- "state:terminal"        done/cancelled
- "archived"              보관 카드
- "state:backlog"         백로그(수령 대상 아님)
- "state:blocked"         차단(why-blocked 대기)
- "state:review"          리뷰 중 — 새 작업 배정 불가(리뷰 자체는 계속)
- "lease_held"            유효 작업 lease = 러너 실행 중(동일 차원, 단일 코드)
- "budget:dispatch-tries>=2" / "budget:attempt>=2"   #1 배정 건너뜀 예산
- "not_auto"              auto 모드인데 auto 라벨 없음(probe 한정)
"""

TERMINAL = ("done", "cancelled")


def budget_reason(i):
    """#1 배정 건너뜀 예산 — dispatch 횟수·실행 회차 상한."""
    if (i.get("dispatches") or 0) >= 2:
        return "budget:dispatch-tries>=2"
    if (i.get("execution_attempt") or 0) >= 2:
        return "budget:attempt>=2"
    return None


def busy_reason(i, *, now="", auto=False):
    """새 작업 배정이 불가하면 reason code, 가능하면 None.

    lease 비교는 ISO 타임스탬프 문자열 비교(서버 전체 관례와 동일) — now가
    비어 있으면 lease 판정을 건너뛴다(테스트 편의).
    """
    if i.get("archived"):
        return "archived"
    st = i.get("state") or ""
    if st in TERMINAL:
        return "state:terminal"
    if st in ("backlog", "blocked", "review"):
        return f"state:{st}"
    if st == "in_progress":
        # 러너 실행 중 = 유효 lease. 만료 lease는 재수령 가능(busy 아님).
        if i.get("lease_expires") and i.get("lease_by") and (not now or i["lease_expires"] > now):
            return "lease_held"
        return None
    if st == "todo":
        if auto and "auto" not in (i.get("labels") or []):
            return "not_auto"
        return budget_reason(i)
    return f"state:{st}" if st else "state:unknown"


def eligible(i, *, now="", auto=False):
    """(배정 가능, reason code) 튜플 — 세 소비자가 공유하는 관문."""
    r = busy_reason(i, now=now, auto=auto)
    return (r is None, r)
