"""에이전트 신원 해석·가족 규칙의 단일 소스 (3QMV, M568PJ79-3QMV).

W0DF 이후 서버(service.resolve_agent_identity)·probe(_alias_resolve 사본·
agent_family 하드코딩)가 각자 규칙을 들고 있었다. 해석 규칙과 가족 어휘를
여기로 모은다 — 각 진입점은 위임만 하고 결과는 기존과 동일(동작 불변).

규칙: 정확 일치 → 별칭(agents.aliases) → 가족(agent_family). 가족 폴백은
스냅샷 소비처(probe decide)에만 의미가 있어 service 진입점은
family_fallback=False로 위임한다 — 기존 서버 해석은 가족 매칭을 하지 않았다.
"""
import os

# 어댑터 별칭 — kanban-adapter는 hermes의 리뷰 대행자다(발령자 결정 2026-10-10)
FAMILY_ALIASES = {"kanban-adapter": "hermes",
                  # cursor-review는 cursor의 읽기전용 리뷰 프로필(별도 에이전트, 가족 동일) — XV84
                  "cursor-review": "cursor"}
REVIEW_FAMILIES = ("codex", "claude", "hermes", "opencode", "cursor")
# host 별칭 — assignee가 user@host 형식이고 base로 가족을 못 찾을 때 host로 추정한다(F2).
# 실제 보드 assignee 2위 형식(skshim@tp-13)은 등록 에이전트명과 달라 가족 정규화에
# 걸리지 않는다. 카드 표 "opencode (tp-13 포함)" — env TT_HOST_FAMILY_MAP으로 확장.
HOST_FAMILY_ALIASES = {"tp-13": "opencode"}


def parse_host_family_map(raw):
    """TT_HOST_FAMILY_MAP='tp-13->opencode,foo->claude' → dict. 빈 칸·불량 항목은 무시."""
    out = {}
    for part in (raw or "").split(","):
        if "->" not in part:
            continue
        k, _, v = part.partition("->")
        k, v = k.strip().lower(), v.strip().lower()
        if k and v:
            out[k] = v
    return out


def effective_host_family_map():
    """기본 host 별칭 + env 확장 — agent_family가 host로 가족을 추정할 때 사용(F2)."""
    return {**HOST_FAMILY_ALIASES, **parse_host_family_map(os.getenv("TT_HOST_FAMILY_MAP", ""))}


def agent_family(name):
    """에이전트 가족 정규화(자기 리뷰 방지 기준) — host 접미사 흡수 + 고정 어휘 prefix.

    어댑터 별칭 우선(kanban-adapter→hermes 대행). base에서 가족을 못 찾고 user@host
    형식이면 host 별칭으로 가족을 추정한다(F2 — skshim@tp-13 → opencode). 미매칭
    (None)은 가족 미지 에이전트(qw-flash, agy 등) — 본인 신원 제외만 적용되고
    가족 제외는 없다.
    """
    s = (name or "").strip().lower()
    base, _, host = s.partition("@")
    if base in FAMILY_ALIASES:
        return FAMILY_ALIASES[base]
    for fam in sorted(REVIEW_FAMILIES, key=len, reverse=True):
        if base.startswith(fam):
            return fam
    if host:
        hmap = effective_host_family_map()
        if host in hmap:
            return hmap[host]
    return None


def alias_resolve(cand, agents):
    """별칭 사전(agent.aliases) 해석 — cand가 어떤 에이전트의 별칭이면 등록명."""
    for a in agents or []:
        if cand and cand in (a.get("aliases") or []):
            return a.get("name")
    return None


def resolve(cand, agents, family_fallback=True):
    """후보 → 등록 에이전트 이름. 정확 일치 → 별칭 → (옵션) 가족 매칭.

    agents는 {"name": ..., "aliases": [...]} dicts — probe는 decide 스냅샷을
    그대로, service는 DB rows를 aliases 파싱 후 넘긴다. family_fallback=False는
    서버 진입점의 기존 규약(별칭까지만 해석) 유지용."""
    s = (cand or "").strip()
    if not s:
        return None
    for a in agents or []:
        if a.get("name") == s:
            return s
    hit = alias_resolve(s, agents)
    if hit:
        return hit
    if not family_fallback:
        return None
    fam = agent_family(s)
    for a in agents or []:
        if fam and agent_family(a.get("name")) == fam:
            return a.get("name")
    return None
