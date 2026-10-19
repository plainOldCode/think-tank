"""게이트 실패 사유 규칙표 (docs/gate-failure-rules.md §4) — 데이터로 운영.

§4.1 운영 규칙: 새 유형은 코드가 아니라 행 추가. 판정 입력은 기계적 조건만
(스냅샷 필드·이벤트 패턴) — 서술형 조건은 금지되고 기계적 조건으로 못 쓰는
행은 unknown으로 간다. 각 행의 기본 대응은 LLM 없이 완결(graceful degrade, §3.2).
LLM 개입은 R+S/R+C의 advisory뿐이고 allow·merge는 어떤 처리 구분에서도 LLM 출력이
아니다(§3.2). 집계 열은 혼합 출처(AGG_SOURCE) — §8과 함께 보존.

wired: 기본 대응이 연결된 위치(file:symbol) — ""는 미연결(데이터만, 연결은 별도
카드/선행 기록 필요). handling "미확인"은 행 자체가 미확정(§8)임을 뜻하는 표기이지
운영 처리 구분이 아니다. 행의 code는 policy 안정 코드와 문자열로만 대응한다(§9).
"""
import os

# 처리 구분 (§3.1) — LLM 개입 정의
HANDLING_R = "R"          # 규칙만
HANDLING_R_S = "R+S"      # 규칙 + LLM 요약(advisory)
HANDLING_H = "H"          # 사람 (통보만 자동)
HANDLING_BUG = "BUG"      # probe 수정으로 제거
HANDLING_C = "C"          # 분류(advisory) + 사람 확인
HANDLING_PENDING = "미확인"  # 행 자체가 미확정(§8) — 운영 처리 구분 아님, 확인 전 표기

# G1DD — 규칙표 버전. 행 추가/수정·연결 변경 시 이 값을 올리고 CHANGELOG에 기록한다.
RULE_VERSION = "gf-r1"

AGG_SOURCE = ("혼합 출처(gate-failure-rules.md §8): 기존 행=413장 코멘트 패턴 추정, dependency=제한된 이벤트 창(seq<=1363, 2026-10-09T13:38~10-11T17:35+0900)의 blocked 전이 관측 0건, marker_mismatch/stale_base=기록 경로 부재로 미확인. WVBY 코멘트 2026-10-11")

FAILURE_RULES = (


    {"code": "observe_failed",
     "judgment": "PR 수집(pr list)이 repo 단위로 실패해 관측이 불완전한 사이클 "
                 "(snap.pr_observed에 failed 존재, snap.degraded에 pr_list) — "
                 "해당 repo 후보 카드에 'PR 없음' 판정 금지",
     "default": "관측 재시도(다음 사이클) — [observe-failed a<n>] 마커 회차당 1회 dedup, "
                "반복 시 §6 사람 통보. 관측 성공 repo의 카드는 기존 판정 유지",
     "handling": HANDLING_R, "wired": "server/probe/providers.py collect_github + "
                                      "server/probe/core.py _pr_list_failed/_decide_observe_failed "
                                      "(TT M58SPT6D-T3YT)",
     "agg": "0(신규, 2026-10-19)", "note": "§12 신규 행 — NVP §3-3. '조회 실패'와 'PR 없음' 분리"},

    {"code": "ci_failed",
     "judgment": "PR checks에 FAILURE 포함(비green)",
     "default": "ci-fix dispatch 유지 — 실패 로그 분류는 advisory(R+S 요약 §5 S1)",
     "handling": HANDLING_R_S, "wired": "server/probe/core.py _decide_in_progress(ci-fix-ip) + tt_lifecycle ci-fix",
     "agg": "3", "note": "모두 9/30, 다수 자가 해소"},


    {"code": "reviewer_session_failed",
     "judgment": "발령 이력 마커 존재+무판정+리뷰어 점유 흔적+관망 창 경과 "
                 "(TT_REVIEW_STALL_MIN) 또는 최신 발령과 같은 dispatch id의 runner 실패 코멘트",
     "default": "회차 마커 [review-retry a<n>]로 재발령 1회(새 세션 강제, fresh) — "
                "N=3 도달 시 needs-human 1회",
     "handling": HANDLING_R, "wired": "server/probe/core.py _reviewer_session_stalled/_maybe_review_retry "
                             "(TT M4M9BTC1-12MZ)",
     "agg": "1(WS3C 실측)", "note": "§12 신규 행 — 2026-10-11 구현"},

    {"code": "evidence_rejected",
     "judgment": "tt-server 시스템 코멘트 'done 증거 없음 또는 실패/미확정 보고' "
                 "(routers/issues DONE_GATE_MSG — done 거부 시 기록됨, comment.added 이벤트 포함)",
     "default": "담당 assignee 재제출 요청 노트(회차당 1회 dedup) — 사유 요약은 advisory(R+S §5 S2)",
     "handling": HANDLING_R_S, "wired": "server/probe/core.py decide ⑤ evidence 스캔 + "
                                        "execute review-note 전용 마커",
     "agg": "45(27카드)", "note": "verify 13건은 재제출 성공. 시스템 코멘트가 기계적 입력"},

    {"code": "verify_422",
     "judgment": "verify 422 — 현재 호출자(cli)만 보고 카드에 기록이 없어 기계적 판정 불가. "
                 "서버 기록 선행 필요",
     "default": "증거 형식 오류 안내(v2.1 3단계 스키마) 후 재시도",
     "handling": HANDLING_R, "wired": "",
     "agg": "1", "note": "기록 계기화가 선행 과제"},

    {"code": "runner_stall",
     "judgment": "runner STALL / STALL-killed 코멘트(러너 장부 기준)",
     "default": "재dispatch, 반복 시 사람 통보(§6 억제 규칙 적용)",
     "handling": HANDLING_R, "wired": "",
     "agg": "약 31", "note": "감시(STALL 통지·kill·상태 기록)만 러너 연결"
                             "(tt-runner.py stall_check) — 재dispatch·반복 에스컬레이션은 미연결"},

    {"code": "budget_exhausted",
     "judgment": "dispatch-tries>=2 (policy 안정 코드 budget_reason)",
     "default": "needs-human 유지 — 완화 금지",
     "handling": HANDLING_R, "wired": "server/probe/core.py decide ③ + execute needs-human",
     "agg": "2", "note": "현행 안전장치 그대로"},

    {"code": "needs_decision",
     "judgment": "level4-notify human 대기(waiting_for=human)",
     "default": "사람에게 올림(notify_hook 에이전트에 notify 명령)",
     "handling": HANDLING_H, "wired": "server/service.py reconcile/notify 경로",
     "agg": "2~3", "note": "RBAC, 멘션 설계 등. go/stop 아님"},

    {"code": "receive_mode_undeclared",
     "judgment": "claim 409 receive_mode_undeclared (RQRT M4MGV3CM) — 등록 에이전트가 "
                 "수신 계약(hook|watcher+interval|relay) 미선언 상태로 claim",
     "default": "선언 안내 1회 후 재시도 — tt agent set NAME receive_mode=... "
                "(watcher는 watcher_interval=분 필수). 비등록 세션은 검증 대상 아님(암묵 relay)",
     "handling": HANDLING_R, "wired": "server/routers/issues.py claim 409 + "
                                      "server/routers/agents.py _validate_receive_mode",
     "agg": "0(신규, 2026-10-12)", "note": "§12 신규 행 — 수신 계약 의무화(409 코드 자체는 변경 금지 §9)"},

    {"code": "lease_conflict_401",
     "judgment": "claim 409 lease_held 또는 hook 401",
     "default": "중복 작업 중단 — 사이클 재판정(마커 없음)",
     "handling": HANDLING_R, "wired": "server/policy.py review_eligible review_occupied + execute 폴백 체인",
     "agg": "4", "note": "409/401 코드 자체는 변경 금지(§9)"},


    {"code": "dependency",
     "judgment": "waiting_for=dependency 필드 — 필드와 notify_blocked_dependents 경로 존재",
     "default": "release_ready 투영(enrich_blocked) + 사람 통보(notify). 참고: probe는 auto+idle+release_ready면 resume→todo 자동 재개 경로 보유(core.py)",
     "handling": HANDLING_H, "wired": "server/service.py enrich_blocked/notify_blocked_dependents",
     "agg": "blocked 전이 0건(실측)", "note": "§8 — waiting_for 필드명 갱신 이벤트 159건(63카드, 상태 이탈 초기화 포함)이나 값 변화는 old/new 부재로 복원 불가. 커버리지 10/9~"},

    {"code": "marker_mismatch",
     "judgment": "마커 불일치 — 정식 기록 경로 부재(§8 실측)",
     "default": "미확정 — 확인 전 운영 처리 없음",
     "handling": HANDLING_PENDING, "wired": "",
     "agg": "미확인(기록 경로 부재)", "note": "기록 부재는 0건의 증거가 아님. stale_base와 분리 유지(§8)"},

    {"code": "stale_base",
     "judgment": "stale head(리뷰 문맥) — 정식 기록 경로 부재(§8 실측)",
     "default": "미확정 — 확인 전 운영 처리 없음",
     "handling": HANDLING_PENDING, "wired": "",
     "agg": "미확인(기록 경로 부재)", "note": "기록 부재는 0건의 증거가 아님. stale-base 게이트는 tt-push 별개 구현(PR#75); marker_mismatch와 분리 유지(§8)"},

    {"code": "unknown",
     "judgment": "위 어느 조건에도 안 맞음",
     "default": "소형 모델이 가장 가까운 코드 제안(advisory rank) + 사람 확인, 판단 로그 필수",
     "handling": HANDLING_C, "wired": "",
     "agg": "모호 약 30+", "note": "§5 C1 + §7 그림자 모드 — 상시 존재가 아니므로 규칙 부분은 무음 완결"},
)


def rules():
    """규칙표 전체 — 행 추가가 확장의 유일한 방식(§4.1). 반환은 새 리스트(방어적)."""
    return [dict(r) for r in FAILURE_RULES]


def get(code):
    """사유 코드 → 행(dict) 또는 None. 알 수 없는 코드는 unknown 행으로 분류한다."""
    for r in FAILURE_RULES:
        if r["code"] == code:
            return dict(r)
    return None


def handling_of(code):
    """사유 코드의 처리 구분 — 미등록 코드는 unknown의 처리 구분(C)을 따른다."""
    r = get(code)
    return (r or get("unknown"))["handling"]


def suppress_n():
    """반복 사건 억제 임계 N(§6) — 같은 (사유코드, 범위) 반복 N회 도달 시 사람 통보
    1회 후 자동 대응 스킵. 해제는 사람 전용. 0이면 억제 비활성."""
    try:
        return max(0, int(os.getenv("TT_SUPPRESS_N", "3")))
    except ValueError:
        return 3
