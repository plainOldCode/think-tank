"""엔티티 정본 카탈로그 (XKRY, M58SPRZ7-XKRY) — 종류별 resolve 단일 진입점.

NVP §3-1: 엔티티 종류별 정본이 흩어져 있고, 카탈로그를 거치지 않은 문자열(코멘트
author 30여 종)이 그대로 신원처럼 쓰인다. resolve는 정본(canonical id) 또는
None을 돌린다. None은 노드로 승격하지 않고 unknown_entity로 센다 — 기록은
kind+원문 id뿐이고 본문은 저장하지 않는다(TV Q4).

정의 (TV §1a):
- card: issues.id — M#######-#### 형식. 대문자 정규화만 하고 케이스 보존 필드는 없다.
- pr: GitHub (owner/repo, number) → "owner/repo#N". repo 대소문자는 원문 보존
  (기존 조인이 정확 문자열 매치 — 정규화하면 계약이 깨진다).
- agent: identity.resolve 위임(사본 금지, 3QMV) — 정확명→별칭→(옵션)가족.
- reviewer: 등록 에이전트 중 독립 리뷰어 — 정확명·별칭까지만(가족 폴백 금지).

이 모듈은 DB를 모른다 — agents 후보는 호출자가 넘긴다(probe 스냅샷·service rows).
"""
import re
from collections import Counter

from identity import resolve as _identity_resolve  # 3QMV 단일 소스 — 사본 금지

ENTITY_KINDS = ("card", "pr", "agent", "reviewer")

# 카드 id — TT 이슈 id(접두-접미 하이픈 구분). 접두 6~12, 접미 3~6.
CARD_ID_RE = re.compile(r"^[A-Z0-9]{6,12}-[A-Z0-9]{3,6}$")
# PR 참조 — owner/repo#N 또는 github PR URL. owner/repo는 [\w.-] (GitHub 규약).
PR_REF_RE = re.compile(r"^([\w.-]+/[\w.-]+)#(\d+)$")
PR_URL_RE = re.compile(r"^https?://github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)/?$")

# unknown_entity — kind별 카운트 + 원문 id 링(본문 저장 금지, TV Q4). 최근 50건.
_unknown_counts = Counter()
_unknown_log = []
UNKNOWN_LOG_LIMIT = 50


def unknown_entity(kind, raw):
    """카탈로그에 없는 문자열 기록 — 노드 승격 금지. kind+원문 id만 남긴다."""
    _unknown_counts[kind] += 1
    rec = {"kind": kind, "raw_id": (raw or "")[:120], "n": _unknown_counts[kind]}
    _unknown_log.append(rec)
    del _unknown_log[:-UNKNOWN_LOG_LIMIT]
    return rec


def unknown_counts():
    return dict(_unknown_counts)


def unknown_log():
    return list(_unknown_log)


def resolve(kind, raw, agents=None, family_fallback=False):
    """종류별 정본 해석. 등록되지 않은 문자열은 None + unknown_entity 기록.

    agents는 {"name": ..., "aliases": [...]} dicts — probe 스냅샷 또는 service rows.
    family_fallback은 agent 종류에서만 의미가 있다(기본 False — 서버 규약 별칭까지만).
    """
    if kind == "card":
        return resolve_card(raw)
    if kind == "pr":
        return resolve_pr(raw)
    if kind == "agent":
        return resolve_agent(raw, agents or [], family_fallback=family_fallback)
    if kind == "reviewer":
        return resolve_reviewer(raw, agents or [])
    raise ValueError(f"unknown entity kind: {kind!r} (ENTITY_KINDS 참조)")


def resolve_card(raw):
    """카드 id 정본 — 공백 제거·대문자 정규화. 형식 불일치는 unknown."""
    s = (raw or "").strip().upper()
    if CARD_ID_RE.match(s):
        return s
    unknown_entity("card", raw)
    return None


def resolve_pr(raw):
    """PR 정본 — "owner/repo#N" 또는 github PR URL → "owner/repo#N"."""
    s = (raw or "").strip()
    m = PR_REF_RE.match(s) or PR_URL_RE.match(s)
    if m:
        return f"{m.group(1)}#{int(m.group(2))}"
    unknown_entity("pr", raw)
    return None


def resolve_agent(cand, agents, family_fallback=False):
    """에이전트 정본 — identity.resolve 위임. 미등록은 None(unknown 기록)."""
    hit = _identity_resolve((cand or "").strip(), agents or [],
                            family_fallback=family_fallback)
    if hit:
        return hit
    unknown_entity("agent", cand)
    return None


def resolve_reviewer(cand, agents):
    """리뷰어 정본 — 등록 에이전트의 정확명·별칭만(가족 폴백 금지, 자기 리뷰 방지 규약)."""
    hit = _identity_resolve((cand or "").strip(), agents or [], family_fallback=False)
    if hit:
        return hit
    unknown_entity("reviewer", cand)
    return None
