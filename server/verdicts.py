"""판정(verdict) 파싱의 단일 소스 (9WWK, M54X0Y56-9WWK) — 세 소비처가 같은 규칙을 쓴다.

- `/rejections/summary` (server/routers/issues.py)
- probe 판정·반려 감지 (server/probe/core.py)
- 리뷰어 보정 지표 (scripts/metrics_collect.py)

규칙(X7DG RC 확정): 첫 줄이 `review: approve|request-changes`로 **정확히** 시작하고
그 줄로 끝나는 댓글만 판정이다(소문자 규약 — 대소문자 구분). 본문 중간의 인용·
에코·보강 메모는 판정이 아니다. 기계 author(runner 에코·시스템·보드)도 판정이
아니다 — 30일 창에서 27%가 비판정으로 집계됐던 원인.
"""
import re

VERDICT_LINE = re.compile(r"review: (approve|request-changes)\s*$")
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
    return m.group(1) if m else None


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


def verdicts_all(comments, reviewers, pr_no, sha8):
    """리뷰어별 최신 판정 사전 {author: verdict} — J2NJ 이중 판정 집계용.

    review_verdict와 동일 자격(author 일치 + PR#n@sha8 head 일치)이지만
    리뷰어마다 최신 판정을 개별 보관한다. 단일 리뷰어 흐름은 기존
    review_verdict(latest-wins)를 그대로 쓴다(동작 불변)."""
    out: dict = {}
    if isinstance(reviewers, str):
        reviewers = [reviewers]
    for c in comments or []:
        if c.get("author") not in (reviewers or []):
            continue
        body = c.get("body") or ""
        m = PR_SHA_RE.search(body)
        if not m or int(m.group(1)) != pr_no or m.group(2).lower() != (sha8 or "").lower():
            continue
        v = first_line_verdict(body)
        if v:
            out[c.get("author")] = v
    return out
