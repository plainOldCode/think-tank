"""probe 마커·증거·억제 판정 규칙(TMXJ-S2).

probe 마커 집계(_probe_marker·_repeat_count)·done 증거 경계(_evidence_unresolved·
_ev_open)·반복 사건 억제 카운터(_suppressed·_ESC_FMT·_UNBLOCK_FMT)·반려 판정
수집(_rejection_verdicts·_correct_loop_sections·369Y/RPXH). 접근 계층 불필요 —
순수 함수군이다(verdicts·identity는 서버 루트 모듈)."""
import re
from datetime import datetime

from verdicts import classify_rejection as _classify_rejection, first_line_verdict  # 9WWK 단일 파서
from verdicts import is_machine_author as _is_machine_author  # 71NK RC — 반려 집계 기계 author 제외


_AUTO_CLASSIFIED_RE = re.compile(r"^\[auto-classified\]\s+label=(\S+)", re.MULTILINE)


CI_BOTS = ("doc-check", "e2e")


_RELAY_OBSERVERS = {"probe", "tt-embedding-daemon", "tt-server", "board"}  # (사유,pr,sha) 카운터 한도 — 도달 시 needs-human 에스컬레이션


def _probe_marker(i, marker):
    return any(marker in (c.get("body") or "") for c in (i.get("comments") or [])
               if c.get("author") == "probe")


def _comment_ts(c):
    """댓글 시각 필드 — wire 형식은 ts(db.comments_of), 테스트/구형 호환 created_at."""
    return c.get("ts") or c.get("created_at") or ""


def _evidence_unresolved(i):
    """현재 회차의 done 거부가 아직 해소되지 않았는지(리뷰 R5).

    증거 경계(evidence_after_comment_id — 회차 리셋 시 갱신) 이후의 시스템
    코멘트만 대상으로 한다. 경계 이전 거부는 이전 회차의 것 — 새 회차에서
    재사용하지 않는다. 거부(DONE_GATE_MSG)와 접수('보고 접수') 중 최신
    (ts, id 순)이 거부면 미해결. 접수된 성공 보고 뒤에 남은 과거 거부는
    재요청하지 않는다. 접수 코멘트 이전 카드 호환으로 reported도 해소로 본다."""
    b = i.get("evidence_after_comment_id") or 0
    cands = []
    for c in (i.get("comments") or []):
        if c.get("author") != "tt-server" or (c.get("id") or 0) <= b:
            continue
        body = c.get("body") or ""
        if body.startswith("done 증거 없음"):
            cands.append(("reject", c.get("ts") or c.get("created_at") or "", c.get("id") or 0))
        elif body.startswith("보고 접수"):
            cands.append(("accept", c.get("ts") or c.get("created_at") or "", c.get("id") or 0))
    if i.get("verification_status") == "reported":
        return False
    if not cands:
        return False
    return max(cands, key=lambda t: (t[1], t[2]))[0] == "reject"


def _suppressed(i, code, scope):
    """(억제 여부, 카운터 재집계 시작 comment id) — 해제 마커가 에스컬레이션보다
    최신이면 해제된 것으로 보고 그 이후만 카운트한다."""
    esc = _ESC_FMT.format(code, scope)
    if not any(esc in (c.get("body") or "") for c in (i.get("comments") or [])):
        return False, 0
    since = 0
    latest = None
    for c in (i.get("comments") or []):
        if c.get("author") in ("probe", "dispatchd"):
            continue  # 사람 전용 해제 — probe 자동 초기화 금지(§6)
        if _UNBLOCK_FMT.format(code, scope) in (c.get("body") or ""):
            k = (c.get("ts") or c.get("created_at") or "", c.get("id") or 0)
            if latest is None or k > latest[0]:
                latest = (k, c.get("id") or 0)
    if latest is None:
        return True, 0
    return False, latest[1]


def _ev_open(i):
    """⑤ 미해결 조건 — tt-server done 거부 미해소 또는 무보고 병합 미접수(JXYS R1).
    판정(decide)과 집행(execute 재확인)이 같은 조건을 써야 독려가 실제 저장된다."""
    cs = i.get("comments") or []
    if any("done 증거 없음" in (c.get("body") or "") for c in cs
           if c.get("author") == "tt-server") and _evidence_unresolved(i):
        return True
    if i.get("state") != "review":
        return False  # 재작업 회귀 카드 — 옛 병합 기록으로 재독려 금지
    mc = max((c for c in cs if c.get("author") == "probe"
              and "merged — 완료 보고 없음" in (c.get("body") or "")),
             key=lambda c: (_comment_ts(c), c.get("id") or 0), default=None)
    if not mc:
        return False
    mt, mid = _comment_ts(mc), mc.get("id") or 0
    return not any(c.get("author") == "tt-server"
                   and "보고 접수" in (c.get("body") or "")
                   and (_comment_ts(c), c.get("id") or 0) > (mt, mid) for c in cs)


def _repeat_count(i, regex, since_id=0):
    """probe 마커 코멘트 중 regex 일치 수(해제 시점 이후만) — (코드, 범위) 카운터."""
    return sum(1 for c in (i.get("comments") or [])
               if c.get("author") == "probe" and (c.get("id") or 0) > since_id
               and re.search(regex, c.get("body") or ""))


def _comment_after(x, dt, mid=0):
    """(ts,id) 순서 판별 — DB 시각이 초 단위라 동초 접수도 id로 구분한다(리뷰 R2)."""
    t = _iso(_comment_ts(x)) if dt else None
    if t is None:
        return False
    if t > dt:
        return True
    if t == dt:
        return (x.get("id") or 0) > (mid or 0)
    return False


def _rejection_verdicts(cur):
    """카드의 request-changes 판정 코멘트(시간순). TT·레거시 PR 경로 공용 형식.

    71NK RC — 기계 author(runner 에코 등)의 첫 줄은 반려 집계에서 제외
    (verdicts.is_machine_author — /rejections/summary와 동일 규칙)."""
    out = []
    for c in cur.get("comments") or []:
        if first_line_verdict(c.get("body")) == "request-changes" \
                and not _is_machine_author(c.get("author")):
            out.append(c)
    return out


def _correct_loop_sections(cur):
    """369Y — 발령문에 붙일 섹션 목록. 반복 유형 구조화 + 3회 반려 premise 강제.

    같은 유형 2회 이상 → 구조화 체크 승격 지시. 카드 누적 반려 3회 이상 →
    premise 재검토("같은 premise의 수정이 두 번 실패하면 premise부터 시험").
    """
    verdicts = _rejection_verdicts(cur)
    if not verdicts:
        return []
    from collections import Counter
    buckets = Counter(_classify_rejection(c.get("body") or "") for c in verdicts)
    sections = []
    repeated = sorted(((b, n) for b, n in buckets.items() if n >= 2 and b != "general"),
                      key=lambda kv: (-kv[1], kv[0]))
    if repeated:
        lines = [f"- {b} 지적 {n}회 반복 — 수정 후 이 유형은 lint/테스트/계약 체크로 "
                 "전환해 같은 지적이 재발하지 않게 한다" for b, n in repeated]
        sections.append("--- 구조화 체크 승격 (369Y) ---\n" + "\n".join(lines))
    if len(verdicts) >= 3:
        sections.append(
            "--- premise 재검토 (369Y) ---\n"
            f"반려 {len(verdicts)}회 — 같은 premise의 수정이 두 번 실패했다면 premise부터 시험하라. "
            "acceptance 정의와 접근 방식 자체를 재검토하고, 유지 근거를 카드 노트로 남긴다.")
    return sections


_ESC_FMT = "[escalated ({}, {})]"


_UNBLOCK_FMT = "[unblocked ({}, {})]"


def _iso(ts):
    try:
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


_RUNNER_FAIL_RE = re.compile(r"runner:\S+ dispatch#(\d+) failed exit=[1-9]")


_DISPATCH_ID_RE = re.compile(r"\(dispatch#(\d+)\)")
