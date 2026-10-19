"""사람 go/stop 결정 마커 파서 (7PRQ, NVP §4-3).

형식(사용자 결정 2026-10-19):
  1행: decision: go|stop
  2행: 판정 대상 subject
  3행: reason_code (policy 안정 enum 토큰 — [a-z0-9_]+)

사람 신원은 author 자기신고로 인정하지 않는다 — 별도 인정 경로
(tt verify --human actor류)에서 넘어온 human=True 호출만 기록으로 남긴다.
형식 위반은 None(거부). 마커는 기록·집계만 하고 판정에 연결되지 않는다.
"""
import re

DECISION_RE = re.compile(r"decision:\s*(go|stop)")
REASON_RE = re.compile(r"[a-z0-9_]+")


def parse_decision(body, *, human=False, actor=None, comment_id=None):
    """사람 결정 마커 → {decision, subject, reason_code, actor, comment_id}.

    human=False(별도 인정 경로 밖 — author 자기신고만 있는 경우)는 결정이
    아니다. 형식 위반(3행 미만·decision enum 밖·빈 subject·reason_code 형식
    불일치)은 None — 거부다."""
    if not human:
        return None
    lines = (body or "").strip().splitlines()
    if len(lines) < 3:
        return None
    m = DECISION_RE.fullmatch(lines[0].strip())
    if not m:
        return None
    subject = lines[1].strip()
    reason = lines[2].strip()
    if not subject or not REASON_RE.fullmatch(reason):
        return None
    return {"decision": m[1], "subject": subject, "reason_code": reason,
            "actor": actor, "comment_id": comment_id}
