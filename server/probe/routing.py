"""에이전트 해석·카운터파트 리뷰어 라우팅(TMXJ-S2).

M4JTJ970-WS3C 룰셋 — 자기 리뷰 방지 가족 어휘는 server/identity.py 단일 소스
(3QMV)을 쓰고, 기본 카운터파트 표(DEFAULT_REVIEW_MAP)와 폴백 체인
(REVIEW_FALLBACK)은 저장 설정(review-routing)으로 덮인다(XV84). 순수 함수군."""
from identity import (FAMILY_ALIASES, REVIEW_FAMILIES, agent_family,  # noqa: F401
                      effective_host_family_map, parse_host_family_map)
from identity import alias_resolve as _identity_alias_resolve
from identity import resolve as _identity_resolve
from .markers import CI_BOTS


DEFAULT_REVIEW_MAP = {"codex": "claude", "claude": "codex",
                      "hermes": "codex", "opencode": "codex"}


REVIEW_FALLBACK = ("agy", "cursor-review", "codex", "claude")


NON_EXECUTORS = ("kanban-adapter", "codex-read-only", "tt-reviewer")


def effective_review_map():
    """기본 카운터파트 표 — decide가 사이클당 1회 산출.

    D3(M5BH92X3-JS7Q): env override 레이어 제거 — 저장 설정
    (settings/review-routing)이 유일한 런타임 override다(_DecideCtx 합성)."""
    return dict(DEFAULT_REVIEW_MAP)


def _receivable(a):
    """등록 에이전트가 dispatch 전달 가능한 상태인지(전달 사다리·재위임용).

    hook/watcher/relay는 base_url(관찰·전달 기준 주소)가 필요하고, poll(M3B5JHGP)은
    runner_name 선언만으로 러너 pending inbox를 통해 수령한다 — 포트 없는
    CLI 에이전트(codex·claude류)의 배정 배제를 막는다. receive_mode 미선언
    hook 에이전트는 구 등록분 호환으로 base_url만으로 전달 가능(선언 강제는 배정 단계).
    """
    if not a.get("enabled"):
        return False
    if (a.get("receive_mode") or "").strip() == "poll":
        return bool((a.get("runner_name") or "").strip())
    return bool(a.get("base_url"))


def _assignable(a):
    """자동 배정 후보 — RQRT 수신 계약 선언까지 요구(미선언 claim 409 루프 예방)."""
    if not (a.get("receive_mode") or "").strip():
        return False
    return _receivable(a)


def _routing_family(name, rmap):
    """라우팅 lookup용 가족 — 매핑 키(최장 prefix) 우선, 다음 고정 가족."""
    base = (name or "").strip().lower().split("@", 1)[0]
    for fam in sorted(set(rmap), key=len, reverse=True):
        if base.startswith(fam):
            return fam
    return agent_family(name)


def _impl_family(assignee):
    """구현 에이전트의 가족 — CI 봇은 구현 에이전트가 아니라 None."""
    a = (assignee or "").strip()
    return None if a.lower() in CI_BOTS else agent_family(a)


def route_reviewer(assignee, rmap):
    """구현 에이전트 → 카운터파트 리뷰어(순수 — 가용성 무관).

    미표기(CI 봇 포함)·미매칭 가족 → codex 기본. codex* 가족 구현은 매핑값(claude)으로 —
    자기 리뷰 방지 원칙이 codex 기본보다 우선(발령자 결정 2026-10-10).
    구현 에이전트 소스는 카드 assignee — 서버 own_work 판정(claim-review 409)과 동일.
    """
    a = (assignee or "").strip()
    if a.lower() in CI_BOTS:
        a = ""
    fam = _routing_family(a, rmap)
    if fam and fam in rmap:
        return rmap[fam]
    return "codex"


def _alias_resolve(cand, agents):
    """별칭 사전 해석 — identity.alias_resolve 위임(3QMV 단일 소스)."""
    return _identity_alias_resolve(cand, agents)


def _resolve_agent(cand, agents):
    """후보 → 실제 dispatch 대상 등록 에이전트 이름. 정확 일치 → 별칭 → 가족 매칭.

    등록 에이전트 스냅샷(decide의 enabled+base_url 필터 통과 집합)에 없으면
    None — 오프라인 판정의 기준. 규칙 본체는 identity.resolve(3QMV).
    """
    return _identity_resolve(cand, agents)


def _resolve_executor(cand, agents):
    """review-fix 발령 대상 해석 — 정확 일치 우선, 가족 매칭은 실행 가능 에이전트 한정.

    PXCJ 리뷰 블로커: _resolve_agent의 가족 매칭은 등록 순서 첫 에이전트를 돌려
    hermes@m2max·hermes가 kanban-adapter(실행 불가)로 해석될 수 있다 — hermes-m2max가
    등록돼 있어도 수정 정체(#343 동형). 정확 일치가 NON_EXECUTOR면 그대로 돌려 실행
    불가 통보 분기로 보내고, 가족 매칭은 NON_EXECUTORS를 제외한 후보만 본다. 리뷰어
    라우팅(pick_reviewer)은 _resolve_agent를 그대로 쓴다 — 리뷰 대행은 의도된 동작.
    """
    for a in agents or []:
        if a.get("name") == cand:
            return cand
    hit = _alias_resolve(cand, agents)
    if hit:
        return hit
    fam = agent_family(cand)
    for a in agents or []:
        if a.get("name") in NON_EXECUTORS:
            continue
        if fam and agent_family(a.get("name")) == fam:
            return a.get("name")
    return None


def pick_reviewer(assignee, agents, busy, rmap, fallback=None):
    """라우팅 + 미가용 폴백(순수). 전부 미가용이면 None — 레거시 폴백으로.

    agents: 등록+활성 에이전트 스냅샷(오프라인 판정), busy: 활성 lease 보유
    에이전트 이름 집합. 리스 보유는 에이전트 단위로만 판정한다(F3 — 가족 한 명의
    lease가 가족 전체 후보를 막지 않는다, 검증기준 ①). 가족 매칭은 자기 리뷰
    가드에만 쓴다. 작성자 본인 신원과 구현 가족(고정 어휘·어댑터 별칭·host 별칭
    포함)은 매핑 확장과 무관하게 끝까지 제외된다 — 자기 리뷰 방지 유지.
    """
    a = (assignee or "").strip()
    impl_fam = _impl_family(a)
    primary = route_reviewer(assignee, rmap)
    # 런타임 설정 우선(XV84) — fallback 인자가 snap["review_routing"]에서 온다
    chain = [primary] + [c for c in (fallback or REVIEW_FALLBACK) if c != primary]
    for cand in chain:
        cand_fam = agent_family(cand)
        if cand == a or (impl_fam and cand_fam == impl_fam):
            continue  # 자기 리뷰 방지 — 본인 신원·구현 가족 후보 제외
        name = _resolve_agent(cand, agents)
        if name is None:
            continue  # 오프라인
        if name in (busy or []):
            continue  # 리스 보유 — 에이전트 단위 판정(F3)
        return name
    return None
