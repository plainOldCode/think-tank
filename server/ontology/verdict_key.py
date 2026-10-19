"""Verdict 노드 투영 (M6V3, RD §4.2) — 정본 키 = (card, attempt, subject_hash).

시기별로 흩어진 판정 키(PR#n@sha8 / TT#ID@aN/hash)를 하나의 규칙으로 투영한다.
- 신 형식: 둘째 줄 `TT#<ID>@a<n>/<hash64>` — 키의 attempt·hash를 그대로 쓴다.
- 구 형식(레거시): 둘째 줄 `PR#<n>@<sha8>` — subject_hash를 알 수 없으므로 키는
  (card, attempt)로 투영하고 PR@sha8은 보조 속성으로 남긴다. 같은 (card, attempt)
  의 레거시 판정은 한 노드로 합쳐진다.

판정어 인식은 server/verdicts.py 공유 파서만 사용한다(71NK — 새 파서 금지).
subject hash가 바뀌면 이전 승인은 무효다: review_subject.verdict()가 정확 일치로
바인딩하므로 이전 Verdict 노드의 키는 새 제출물의 키와 절대 같지 않다(supersedes).
"""
import re

from verdicts import first_line_verdict, PR_SHA_RE

TT_TOKEN_RE = re.compile(r"TT#(\S+)@a(\d+)/([0-9a-f]{64})")


def project(card_id, attempt, body, author, comment_id=None):
    """판정 코멘트 → Verdict 노드 dict. 판정어·바인딩 토큰이 없으면 None."""
    verdict = first_line_verdict(body or "")
    if not verdict:
        return None
    lines = (body or "").strip().splitlines()
    token_line = lines[1].strip() if len(lines) > 1 else ""
    m = TT_TOKEN_RE.fullmatch(token_line)
    if m:
        return {"card": card_id, "attempt": int(m[2]), "subject_hash": m[3],
                "verdict": verdict, "author": author, "comment_id": comment_id,
                "legacy": False}
    pm = PR_SHA_RE.search(token_line)
    if pm:
        return {"card": card_id, "attempt": int(attempt or 0), "subject_hash": None,
                "verdict": verdict, "author": author, "comment_id": comment_id,
                "legacy": True, "pr": int(pm[1]), "sha8": pm[2].lower()}
    return None


def node_key(node):
    """Verdict 노드의 정본 키 — (card, attempt, subject_hash). 레거시는 hash None."""
    return (node["card"], node["attempt"], node["subject_hash"])
