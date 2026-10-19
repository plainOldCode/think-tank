"""카운터파트 리뷰어 라우팅 (M4JTJ970-WS3C) — docs/review-gate.md 룰셋.

리뷰는 작성 에이전트의 카운터파트(상대편)가 맡는다 — 자기 작업 자기 리뷰 방지.
- codex→claude, hermes/opencode/claude→codex (기본 표, 가족 prefix 정규화)
- 미표기·미매칭→codex 기본, codex* 가족 구현→claude (자기 리뷰 방지 우선)
- 자기 리뷰 방지 가드는 고정 가족 어휘+어댑터 별칭(kanban-adapter≡hermes)+본인
  신원 — TT_REVIEW_MAP 확장으로 우회 불가
- 미가용 폴백: agy→codex→claude, 구현 가족 제외 (발령자 결정 2026-10-12)
- 수령 인계: probe가 대신 claim-review — 거부(409)면 다음 후보, 전원 거부·활성
  점유면 마커 없이 다음 사이클 재시도
"""
import pytest

from probe import core as dispatchd
import probe.core
from probe.core import agent_family, pick_reviewer, route_reviewer

NOW = "2026-10-10T10:00:00+0900"
RMAP = probe.core.DEFAULT_REVIEW_MAP
AGENTS_ALL = ("codex", "claude", "agy", "kanban-adapter", "opencode-tp13", "hermes@host")


def _agents(*names):
    return [{"name": n, "enabled": 1, "base_url": "http://x/hook",
             "receive_mode": "hook"} for n in names]


# --- route_reviewer: 기본 표 + 가족 정규화 ---

def test_기본표_카운터파트():
    assert route_reviewer("codex", RMAP) == "claude"
    assert route_reviewer("hermes", RMAP) == "codex"
    assert route_reviewer("opencode", RMAP) == "codex"
    assert route_reviewer("claude", RMAP) == "codex"


def test_host_접미사는_가족으로_흡수된다():
    # #3e — codex vs codex@host 혼재 ID를 가족 단위로 판정
    assert agent_family("codex@host") == "codex"
    assert agent_family("hermes@host") == "hermes"
    assert agent_family("opencode-tp13") == "opencode"
    assert route_reviewer("codex@host", RMAP) == "claude"


def test_host_별칭으로_가족을_추정한다():
    # F2 — 실제 보드 assignee 2위 형식(skshim@tp-13): base 미매칭이면 host로 추정
    assert agent_family("skshim@tp-13") == "opencode"
    assert route_reviewer("skshim@tp-13", RMAP) == "codex"


def test_TT_HOST_FAMILY_MAP으로_host_별칭_확장(monkeypatch):
    monkeypatch.setenv("TT_HOST_FAMILY_MAP", "macbookpro->hermes")
    assert agent_family("skshim@macbookpro") == "hermes"
    assert agent_family("skshim@unknown-host") is None


def test_어댑터는_헤르메스_대행_가족이다():
    assert agent_family("kanban-adapter") == "hermes"


def test_미표기와_미매칭은_codex_기본이다():
    # 검증기준 2 — 매핑 미매칭(미표기 포함)은 codex 리뷰 기본
    assert route_reviewer("", RMAP) == "codex"
    assert route_reviewer(None, RMAP) == "codex"
    assert route_reviewer("qw-flash", RMAP) == "codex"
    assert route_reviewer("agy", RMAP) == "codex"


def test_codex가족_변형은_claude로_대체된다():
    # 검증기준 2 — codex* 가족 구현+미매칭 조합은 claude (자기 리뷰 방지 우선)
    assert route_reviewer("codex-labs", RMAP) == "claude"
    assert route_reviewer("codex@host", RMAP) == "claude"


def test_ci봇은_구현_에이전트가_아니다():
    assert route_reviewer("doc-check", RMAP) == "codex"
    assert route_reviewer("e2e", RMAP) == "codex"
    assert agent_family("doc-check") is None


def test_TT_REVIEW_MAP으로_override와_가족_확장(monkeypatch):
    monkeypatch.setenv("TT_REVIEW_MAP", "codex->tt-reviewer, agy->claude")
    m = probe.core.effective_review_map()
    assert route_reviewer("codex", m) == "tt-reviewer"
    assert route_reviewer("agy", m) == "claude"
    assert route_reviewer("hermes", m) == "codex"  # 기본 표 잔존


# --- 자기 리뷰 방지 가드: 고정 어휘·어댑터 별칭·본인 신원 (리뷰 R2) ---

def test_작성자_본인은_후보에서_제외된다():
    # 가족 미지(kanban-adapter)라도 본인 신원은 정확 일치로 제외 — own_work 409 선제 차단
    assert pick_reviewer("kanban-adapter", _agents("kanban-adapter"), set(), RMAP) is None
    # 본인 제외 후 남은 후보(codex)로는 지목된다 — 제외는 본인에만 적용
    assert pick_reviewer("kanban-adapter",
                         _agents("kanban-adapter", "codex"), set(), RMAP) == "codex"


def test_헤르메스_구현엔_대행_어댑터도_제외된다():
    # codex·claude 미가용이면 kanban-adapter(hermes 대행)도 못 건다 — 자기 가족 제외 유지
    assert pick_reviewer("hermes@host", _agents("kanban-adapter"), set(), RMAP) is None
    assert pick_reviewer("hermes@host",
                         _agents("kanban-adapter", "codex"), set(), RMAP) == "codex"


def test_TT_REVIEW_MAP으로_가족_우회_자기리뷰_불가(monkeypatch):
    # 매핑 키(codex-labs)가 최장 prefix로 codex와 갈라져도 제외 가드는 고정 어휘 기준
    monkeypatch.setenv("TT_REVIEW_MAP", "codex-labs->codex")
    m = probe.core.effective_review_map()
    assert route_reviewer("codex-labs", m) == "codex"
    assert pick_reviewer("codex-labs", _agents("codex"), set(), m) is None
    assert pick_reviewer("codex-labs", _agents("codex", "claude"), set(), m) == "claude"


# --- pick_reviewer: 미가용 폴백 (검증기준 — 폴백 순서 회귀) ---

def test_1순위_오프라인이면_폴백한다():
    agents = _agents("claude", "kanban-adapter", "opencode-tp13")  # codex 오프라인
    assert pick_reviewer("opencode-tp13", agents, set(), RMAP) == "claude"


def test_폴백순서는_agy_다음_codex_다음_claude다():
    # codex 구현 → claude 1순위(카운터파트 표). claude 오프라인 → agy
    agents = _agents("codex", "agy")
    assert pick_reviewer("codex", agents, set(), RMAP) == "agy"
    # agy도 오프라인 → codex는 자기 리뷰라 제외 → claude가 남지만 이미 오프라인 → None
    assert pick_reviewer("codex", _agents("codex"), set(), RMAP) is None
    # opencode 구현 → codex 1순위 오프라인 → agy → agy도 오프라인 → claude
    agents2 = _agents("opencode-tp13", "claude")
    assert pick_reviewer("opencode-tp13", agents2, set(), RMAP) == "claude"


def test_폴백에서도_구현가족은_제외된다():
    # codex 구현: claude·kanban-adapter 모두 오프라인 → codex는 자기 리뷰라 제외 → None
    agents = _agents("codex", "opencode-tp13")
    assert pick_reviewer("codex", agents, set(), RMAP) is None
    assert pick_reviewer("codex", agents + _agents("claude"), set(), RMAP) == "claude"


def test_리스_보유_리뷰어는_미가용이다():
    agents = _agents(*AGENTS_ALL)
    assert pick_reviewer("codex", agents, {"claude"}, RMAP) == "agy"
    assert pick_reviewer("opencode-tp13", agents, {"codex"}, RMAP) == "agy"


def test_전원_미가용이면_None_레거시_폴백으로_흐른다():
    assert pick_reviewer("codex", [], set(), RMAP) is None
    assert pick_reviewer("codex", _agents("codex"),
                         {"claude", "agy", "codex"}, RMAP) is None


def test_라우팅은_작성자_자신의_가족을_지목하지_않는다():
    # 검증기준 3 — own_work 409(서버 강제)의 probe 측 전제: 라우팅이 스스로를 안 건다
    agents = _agents(*AGENTS_ALL)
    for impl in ("codex", "codex@host", "claude", "hermes@host", "kanban-adapter",
                 "opencode-tp13", "", "qw-flash"):
        routed = pick_reviewer(impl, agents, set(), RMAP)
        assert routed != impl
        assert agent_family(routed) != agent_family(impl), impl


# --- decide 배선: 양 경로 모두 카운터파트/가족 정책 (리뷰 R1) ---


def gate_on(monkeypatch):
    monkeypatch.setenv("TT_REVIEW_AGENT", "kanban-adapter")  # 게이트 on — env는 레거시 폴백


# --- execute 배선: 동기 수령 인계, 409 폴백, 레거시 가드 ---


# --- XV84(M4W99A6Z): cursor-review 리뷰어 편입 + 설정 기반 폴백 순서 ---

def test_cursor_review는_cursor_가족이다():
    # cursor-review는 구현 cursor의 별도 리뷰 프로필 — 가족 정규화로 자기 리뷰 가드 유지
    assert probe.core.agent_family("cursor-review") == "cursor"
    assert probe.core.agent_family("cursor") == "cursor"


def test_기본_폴백에_cursor_review가_둘째다():
    assert probe.core.REVIEW_FALLBACK[0] == "agy"
    assert probe.core.REVIEW_FALLBACK[1] == "cursor-review"


def test_설정으로_폴백순서_런타임_교체():
    # 저장 설정(snap["review_routing"])이 코드 기본을 덮는다 — 재배포 없이 순서 조정
    agents = _agents("codex", "cursor-review", "agy")
    fb = ("cursor-review", "agy", "codex", "claude")
    assert pick_reviewer("codex", agents, set(), RMAP, fallback=fb) == "cursor-review"
    # 설정은 체인을 '교체'한다 — claude(1순위 라우팅)·codex(자기 리뷰)만 남으면 None
    fb2 = ("claude", "codex")
    assert pick_reviewer("codex", _agents("codex", "agy"), set(), RMAP,
                         fallback=fb2) is None


def test_설정의_review_map도_런타임_교체():
    agents = _agents("codex", "cursor-review", "agy", "claude")
    rmap = {**RMAP, "codex": "cursor-review"}
    assert pick_reviewer("codex", agents, set(), rmap) == "cursor-review"


def test_설정_폴백에도_자기리뷰_가드_유지():
    # cursor 구현에 cursor-review는 가족 동반 — 설정이 1순위로 밀어도 제외된다
    agents = _agents("cursor", "cursor-review", "agy")
    fb = ("cursor-review", "agy")
    assert pick_reviewer("cursor", agents, set(), RMAP, fallback=fb) == "agy"


# --- U2(M5A4HY52-874M): 저장 설정이 TT 경로에도 적용된다 ---

def _tt_snap(issue, *names):
    return {"auto": True, "now": NOW, "issues": [issue], "prs": [],
            "agents": [{"name": n, "enabled": True, "base_url": "http://x/hook",
                        "receive_mode": "hook"} for n in names]}


def test_저장_설정_review_map이_tt경로_대상에_반영된다(client):
    """U2 수용기준 1 — 저장 review_map이 TT 경로 tt-review 대상 선택에 반영된다.
    기존 plan은 effective_review_map()을 직접 불러 저장 설정을 무시했다(RED)."""
    from test_tt_lifecycle import submitted
    issue = submitted(client)
    s = _tt_snap(issue, "codex", "claude")
    s["review_routing"] = {"review_map": {"worker": "claude"}}
    tt = [a for a in dispatchd.decide(s) if a["action"] == "tt-review"]
    assert tt and tt[0]["agent"] == "claude"


def test_저장_설정_review_fallback이_tt경로에_적용된다(client):
    """저장 review_fallback도 TT 경로에 적용된다 — 코드 기본 폴백 대신."""
    from test_tt_lifecycle import submitted
    issue = submitted(client)
    s = _tt_snap(issue, "codex", "claude")
    s["review_routing"] = {"review_map": {"worker": "ghost"},
                           "review_fallback": ["claude"]}
    tt = [a for a in dispatchd.decide(s) if a["action"] == "tt-review"]
    assert tt and tt[0]["agent"] == "claude"
