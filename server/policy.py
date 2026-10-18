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
- "no_verify"             auto 모드인데 brief_verify(정확한 명령) 비어 있음(P8MN)
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


# 라벨 의미 클래스 표(FHH8): 어휘 → 클래스 → 판정 지점. 표에 없는 라벨은 무시한다
# (자유 문자열 라벨과 충돌하지 않게 접두어/정확 일치 규칙만 사용 — 기존 동작 불변).
# 어휘는 사람이 결정했다(카드 M4S94ARA-FHH8 코멘트 2026-10-13):
# 고위험 = 'risk:*' 접두어(기존 security 12건 소급 없음), 기존 4개 어휘는 선언만.
LABEL_SEMANTICS = {
    "low_risk": {
        "exact": ("docs", "documentation", "i18n", "문서", "번역"),
        "used_at": "decide() ⓪ 병합 게이트 — 계약 없는 무인 병합 허용(M4DEK6WC-MQYS)",
    },
    "high_risk": {
        "prefix": ("risk:",),
        "used_at": "decide() ⓪ 병합 게이트 + tt_lifecycle tt-deliver — "
                   "v2.1 보고 강제 + probe 자동병합 휴면, 사람 병합 승인 대기(FHH8)",
    },
    # 선언 전용 — 기존 코드가 하드코딩으로 판정하는 어휘(연결 변경 없음, 문서화만).
    "auto": {"exact": ("auto",), "used_at": "policy.eligible() not_auto + no_verify 게이트(P8MN)"},
    "self": {"exact": ("self",), "used_at": "자기발의 구분"},
    "close": {"exact": ("close",), "used_at": "done 승인 예외(routers/issues.py)"},
    "needs-human": {"exact": ("needs-human",), "used_at": "자동화 정지(probe/eligible)"},
}

# 기존 공개 상수 — 표의 low_risk 항목과 단일 출처(기존 임포트 호환 유지).
LOW_RISK_LABELS = LABEL_SEMANTICS["low_risk"]["exact"]


def labels_in_class(labels, cls):
    """라벨 컬렉션에서 클래스 규칙에 걸린 어휘 목록. 미선언 라벨은 무시."""
    rule = LABEL_SEMANTICS.get(cls) or {}
    exact = rule.get("exact") or ()
    prefix = rule.get("prefix") or ()
    out = []
    for x in labels or []:
        s = str(x).strip().lower()
        if s in exact or any(s.startswith(p) for p in prefix):
            out.append(s)
    return out


def is_high_risk(i):
    """카드 라벨 기준 고위험 판정 — 'risk:*' 접두어(FHH8). 라벨 문자열 대소문자 무시."""
    return bool(labels_in_class(_labels(i), "high_risk"))


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
        # P8MN(M5283KGX-P8MN): auto pull은 VERIFY(정확한 명령) 없는 카드를
        # spawn하지 않는다 — "missing fields are a refuse-to-spawn condition".
        if auto and not (_get(i, "brief_verify") or "").strip():
            return "no_verify"
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


# J2NJ(M54V6KY6-J2NJ) — 이중 판정: agents.model 컬럼 → 모델 계열 파생.
# 계열 상호보완이 목적이므로 계정 가족(agent_family)과 무관한 모델 기준 판정이다.
_MODEL_FAMILY_RULES = (
    ("gpt", "gpt"), ("o1", "gpt"), ("o3", "gpt"), ("o4", "gpt"),
    ("claude", "claude"),
    ("gemini", "gemini"),
    ("qwen", "qwen"),
    ("cursor", "cursor"),
    ("deepseek", "deepseek"),
    ("llama", "llama"),
    ("mistral", "mistral"),
)


def model_family(model):
    """agents.model 문자열 → 모델 계열(gpt/qwen/gemini/claude/...). 미지 모델은 None."""
    m = (model or "").lower()
    for kw, fam in _MODEL_FAMILY_RULES:
        if kw in m:
            return fam
    return None


def dual_review_eligible(i):
    """이중 판정 트리거 — 카드 라벨 dual_review(발령자가 생성 시 지정).

    probe 휴리스틱(server/·마이그레이션·인증 경로)은 diff 관측이 필요해 2단계 분리
    (카드 설계 노트 2026-10-18)."""
    return "dual_review" in {str(x).strip().lower() for x in (i.get("labels") or [])
                             if isinstance(x, str)}
