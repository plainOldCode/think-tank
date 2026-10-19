"""판정(verdict) 파싱의 단일 소스 (9WWK, M54X0Y56-9WWK) — 세 소비처가 같은 규칙을 쓴다.

- `/rejections/summary` (server/routers/issues.py)
- probe 판정·반려 감지 (server/probe/core.py)
- 리뷰어 보정 지표 (scripts/metrics_collect.py)

규칙(X7DG RC 확정, 71NK 완화): 첫 줄이 `review:` + 공백 + 판정어로 **시작**하고
판정어 뒤가 단어 경계(공백·구두점·줄끝)인 댓글만 판정이다(대소문자 무시 —
리뷰어가 같은 줄에 사유를 덧붙이거나 대문자로 써도 판독된다). 본문 중간의 인용·
에코·보강 메모는 판정이 아니다(첫 줄 한정 유지). 기계 author(runner 에코·
시스템·보드)도 판정이 아니다 — 30일 창에서 27%가 비판정으로 집계됐던 원인.
"""
import re

VERDICT_LINE = re.compile(r"review:\s*(approve|request-changes)\b", re.IGNORECASE)
# head-bound 판정 표기 'PR#<n>@<sha8>' — probe review_verdict와 동일 규격
PR_SHA_RE = re.compile(r"PR#(\d+)@([0-9a-fA-F]{8})")
MACHINE_AUTHOR = re.compile(r"^(probe|tt-server|tt-embedding-daemon|board)$|^runner@")

REJECT_BUCKETS = {
    "migration": ("마이그레이션", "migration", "컬럼", "column"),
    "test": ("테스트", "test", "회귀", "red", "커버리지", "coverage"),
    "contract": ("계약", "contract", "스키마", "schema", "api.md", "패리티",
                 "문서", "docs", "패러티"),
    "security": ("보안", "security", "secret", "인증", "auth", "권한"),
    "style": ("스타일", "style", "네이밍", "naming", "컨벤션", "lint", "포맷"),
}


def first_line_verdict(body):
    """본문만으로 판정 판독 — author를 모를 때(probe review_verdict 등)."""
    lines = (body or "").strip().splitlines()
    if not lines:
        return None
    m = VERDICT_LINE.match(lines[0].strip())
    return m.group(1).lower() if m else None


def is_machine_author(author):
    return bool(author) and bool(MACHINE_AUTHOR.match(author.strip().lower()))


def verdict_of(author, body):
    """author+본문 판정 — 기계 author는 None(집계 제외)."""
    if is_machine_author(author):
        return None
    return first_line_verdict(body)


def classify_rejection(body):
    """반려 판정 본문 → 버킷. 가장 먼저 걸린 버킷 하나만(다중 지적은 반복 카운트가 흡수)."""
    text = (body or "").lower()
    for bucket, kws in REJECT_BUCKETS.items():
        if any(k.lower() in text for k in kws):
            return bucket
    return "general"

