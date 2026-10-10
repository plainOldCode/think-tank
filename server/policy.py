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

import json

TERMINAL = ("done", "cancelled")

# 저위험 클래스(M4DEK6WC-MQYS): 문서·번역 라벨 카드/PR — 계약 없이도 독립 리뷰를
# 받을 수 있고, CI green+approve면 무인 병합된다. 라벨은 저위험 클래스 '선택'일 뿐
# 게이트 대체가 아니다(독립 리뷰+CI green은 여전히 필수 — codex 의견 반영).
LOW_RISK_LABELS = ("docs", "documentation", "i18n", "문서", "번역")


def _labels(i):
    v = _get(i, "labels")
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except (ValueError, TypeError):
            return []
    return [str(x).strip().lower() for x in (v or [])]


def is_low_risk(i):
    """카드 라벨 기준 저위험 판정 — 라벨 문자열 대소문자 무시."""
    return any(x in LOW_RISK_LABELS for x in _labels(i))


def _get(i, k, default=None):
    """dict/sqlite3.Row 공용 접근 — 라우터가 Row를 그대로 넘길 수 있게."""
    try:
        return i[k] if i[k] is not None else default
    except (KeyError, IndexError):
        return default


def budget_reason(i):
    """#1 배정 건너뜀 예산 — dispatch 횟수·실행 회차 상한."""
    if (_get(i, "dispatches") or 0) >= 2:
        return "budget:dispatch-tries>=2"
    if (_get(i, "execution_attempt") or 0) >= 2:
        return "budget:attempt>=2"
    return None


def busy_reason(i, *, now="", auto=False):
    """새 작업 배정이 불가하면 reason code, 가능하면 None.

    lease 비교는 ISO 타임스탬프 문자열 비교(서버 전체 관례와 동일) — now가
    비어 있으면 lease 판정을 건너뛴다(테스트 편의).
    """
    if _get(i, "archived"):
        return "archived"
    st = _get(i, "state") or ""
    if st in TERMINAL:
        return "state:terminal"
    if st in ("backlog", "blocked", "review"):
        return f"state:{st}"
    if st == "in_progress":
        # 러너 실행 중 = 유효 lease. 만료 lease는 재수령 가능(busy 아님).
        if _get(i, "lease_expires") and _get(i, "lease_by") and (not now or i["lease_expires"] > now):
            return "lease_held"
        return None
    if st == "todo":
        if auto and "auto" not in (_get(i, "labels") or []):
            return "not_auto"
        return budget_reason(i)
    return f"state:{st}" if st else "state:unknown"


def eligible(i, *, now="", auto=False):
    """(배정 가능, reason code) 튜플 — 세 소비자가 공유하는 관문."""
    r = busy_reason(i, now=now, auto=auto)
    return (r is None, r)


def review_eligible(i, *, now="", agent="", low_risk=None):
    """리뷰 수령(claim-review) 가능 여부 — (가능, reason code).

    교차리뷰 전용 판정: 새 작업 배정과 달리 state:review가 '대상'이다.
    - own_work        본인이 만든/작업 중인 카드(교차리뷰 원칙 위반)
    - review_occupied  다른 리뷰어의 유효 점유 lease
    - no_contract      계약 없는 카드 — 토론·상징 카드 제외
    """
    if _get(i, "archived"):
        return False, "archived"
    st = _get(i, "state") or ""
    if st in TERMINAL:
        return False, "state:terminal"
    if st != "review":
        return False, f"state:{st}"
    if agent and _get(i, "assignee") == agent:
        return False, "own_work"
    rev = _get(i, "reviewer")
    if rev and rev != agent and _get(i, "lease_by") == rev \
            and _get(i, "lease_expires") and (not now or i["lease_expires"] > now):
        return False, "review_occupied"
    if not _get(i, "work_contract") and not (is_low_risk(i) or low_risk):
        # 저위험(문서·번역) 라벨 카드는 계약 없이도 독립 리뷰 가능(M4DEK6WC-MQYS).
        # low_risk 인자: PR 라벨만으로 후보가 된 경우(decide에서 산출) — 카드 라벨과
        # 동일한 단일 관문을 태우기 위한 주입값.
        return False, "no_contract"
    return True, None
