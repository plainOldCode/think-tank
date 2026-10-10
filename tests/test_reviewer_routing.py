"""카운터파트 리뷰어 라우팅 (M4JTJ970-WS3C) — docs/review-gate.md 룰셋.

리뷰는 작성 에이전트의 카운터파트(상대편)가 맡는다 — 자기 작업 자기 리뷰 방지.
- codex→claude, hermes/opencode/claude→codex (기본 표, 가족 prefix 정규화)
- 미표기·미매칭→codex 기본, codex* 가족 구현→claude (자기 리뷰 방지 우선)
- 미가용 폴백: claude→kanban-adapter(hermes 어댑터)→codex, 구현 가족 제외
- 라우팅은 작성자 자신의 가족을 지목하지 않는다 — own_work 409 불변
"""
import pytest

import dispatchd
import probe.core
from probe.core import agent_family, pick_reviewer, route_reviewer

SHA8 = "b" * 8
NOW = "2026-10-10T10:00:00+0900"
RMAP = probe.core.DEFAULT_REVIEW_MAP
AGENTS_ALL = ("codex", "claude", "kanban-adapter", "opencode-tp13", "hermes@host")


def _agents(*names):
    return [{"name": n, "enabled": 1, "base_url": "http://x/hook"} for n in names]


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


def test_TT_REVIEW_MAP으로_override와_가족_확장(monkeypatch):
    monkeypatch.setenv("TT_REVIEW_MAP", "codex->tt-reviewer, agy->claude")
    m = probe.core.effective_review_map()
    assert route_reviewer("codex", m) == "tt-reviewer"
    assert route_reviewer("agy", m) == "claude"
    assert route_reviewer("hermes", m) == "codex"  # 기본 표 잔존


# --- pick_reviewer: 미가용 폴백 (검증기준 — 폴백 순서 회귀) ---

def test_1순위_오프라인이면_폴백한다():
    agents = _agents("claude", "kanban-adapter", "opencode-tp13")  # codex 오프라인
    assert pick_reviewer("opencode-tp13", agents, set(), RMAP) == "claude"


def test_폴백순서는_claude_다음_kanban_어댑터다():
    # codex 구현 → claude 1순위. claude 오프라인 → hermes 자리=kanban 어댑터
    agents = _agents("codex", "kanban-adapter")
    assert pick_reviewer("codex", agents, set(), RMAP) == "kanban-adapter"


def test_폴백에서도_구현가족은_제외된다():
    # codex 구현: claude·kanban-adapter 모두 오프라인 → codex는 자기 리뷰라 제외 → None
    agents = _agents("codex", "opencode-tp13")
    assert pick_reviewer("codex", agents, set(), RMAP) is None
    assert pick_reviewer("codex", agents + _agents("claude"), set(), RMAP) == "claude"


def test_리스_보유_리뷰어는_미가용이다():
    agents = _agents(*AGENTS_ALL)
    assert pick_reviewer("codex", agents, {"claude"}, RMAP) == "kanban-adapter"
    assert pick_reviewer("opencode-tp13", agents, {"codex"}, RMAP) == "claude"


def test_전원_미가용이면_None_레거시_폴백으로_흐른다():
    assert pick_reviewer("codex", [], set(), RMAP) is None
    assert pick_reviewer("codex", _agents("codex"),
                         {"claude", "kanban-adapter"}, RMAP) is None


def test_라우팅은_작성자_자신의_가족을_지목하지_않는다():
    # 검증기준 3 — own_work 409(서버 강제)의 probe 측 전제: 라우팅이 스스로를 안 건다
    agents = _agents(*AGENTS_ALL)
    for impl in ("codex", "codex@host", "claude", "hermes@host",
                 "opencode-tp13", "", "qw-flash"):
        routed = pick_reviewer(impl, agents, set(), RMAP)
        assert agent_family(routed) != agent_family(impl), impl


# --- decide 배선: review-request 액션에 라우팅 리뷰어 ---

def _i(**kw):
    base = {"id": "M4JTJ970-WS3C", "state": "review", "archived": 0, "version": 3,
            "assignee": "codex", "reviewer": "", "lease_by": "", "lease_expires": None,
            "labels": [], "dispatches": 1, "execution_attempt": 1,
            "work_contract": {"version": "v2"}, "comments": [],
            "updated_at": "2026-10-10T09:00:00+0900", "title": "M4JTJ970-WS3C"}
    base.update(kw)
    return base


def _occupied(**kw):
    """모든 등록 에이전트의 교차 수령(review-claim)만 막는 리뷰 점유 — 라우팅 경로 강제."""
    return _i(reviewer="tt-reviewer", lease_by="tt-reviewer",
              lease_expires="2026-10-10T11:00:00+0900", **kw)


def _snap(issues, prs=(), agents=()):
    return {"auto": True, "now": NOW, "agents": list(agents),
            "issues": list(issues), "prs": list(prs)}


def _pr(number=9, sha="b" * 40):
    return [{"number": number, "repo": "plainOldCode/think-tank",
             "checks": [{"state": "SUCCESS"}],
             "branch": "tt/M4JTJ970-WS3C-x", "title": "", "head_sha": sha}]


def _req(acts):
    return [a for a in acts if a["action"] == "review-request"]


@pytest.fixture(autouse=True)
def gate_on(monkeypatch):
    monkeypatch.setenv("TT_REVIEW_AGENT", "kanban-adapter")  # 게이트 on — env는 레거시 폴백


def test_decide는_codex_구현_카드를_claude로_라우팅한다():
    # 검증기준 1
    acts = dispatchd.decide(_snap([_occupied()], _pr(), _agents(*AGENTS_ALL)))
    reqs = _req(acts)
    assert len(reqs) == 1
    assert reqs[0]["reviewer"] == "claude"
    assert not [a for a in acts if a["action"] == "merge"]


@pytest.mark.parametrize("impl", ["hermes@host", "opencode-tp13", "claude"])
def test_decide는_hermes_opencode_claude_구현을_codex로_라우팅한다(impl):
    # 검증기준 1
    acts = dispatchd.decide(_snap([_occupied(assignee=impl)], _pr(), _agents(*AGENTS_ALL)))
    assert _req(acts)[0]["reviewer"] == "codex"


def test_decide는_미표기_카드를_codex_기본으로_라우팅한다():
    acts = dispatchd.decide(_snap([_occupied(assignee="")], _pr(), _agents(*AGENTS_ALL)))
    assert _req(acts)[0]["reviewer"] == "codex"


def test_decide는_codex가족_변형도_claude로_라우팅한다():
    for impl in ("codex@host", "codex-labs"):
        acts = dispatchd.decide(_snap([_occupied(assignee=impl)], _pr(), _agents(*AGENTS_ALL)))
        assert _req(acts)[0]["reviewer"] == "claude"


def test_decide는_qw_flash를_codex_기본으로_라우팅한다():
    acts = dispatchd.decide(_snap([_occupied(assignee="qw-flash")], _pr(), _agents(*AGENTS_ALL)))
    assert _req(acts)[0]["reviewer"] == "codex"


def test_decide는_전원_미가용이면_reviewer_키_없이_내보낸다():
    # 레거시 폴백 — execute가 env(TT_REVIEW_AGENT)→kanban-adapter로 해석
    acts = dispatchd.decide(_snap([_occupied()], _pr(), []))
    reqs = _req(acts)
    assert len(reqs) == 1
    assert "reviewer" not in reqs[0]
    assert "kanban-adapter" in reqs[0]["reason"]


# --- execute 배선: 라우팅 리뷰어로 dispatch, env는 레거시 폴백 ---

class Rec:
    """api 더블 — GET 카드 반환, dispatch/코멘트 기록."""

    def __init__(self, card):
        self.card = card
        self.dispatches = []
        self.commented = []

    def __call__(self, url, path, method="GET", body=None):
        if method == "GET":
            return dict(self.card)
        if path.endswith("/dispatch") and method == "POST":
            self.dispatches.append(body)
            return {"ok": True}
        self.commented.append(body)
        return {"ok": True}


def _act(**kw):
    return {"agent": "probe", "issue": "M4JTJ970-WS3C", "action": "review-request",
            "pr": 9, "repo": "plainOldCode/think-tank", "branch": "tt/x",
            "head_sha": "b" * 40, "marker": f"[review-req #9/{SHA8}]", "reason": "r", **kw}


def test_execute는_라우팅된_리뷰어로_dispatch한다(monkeypatch):
    card = {"id": "M4JTJ970-WS3C", "state": "review", "version": 3, "comments": []}
    rec = Rec(card)
    monkeypatch.setattr(probe.core, "api", rec)
    probe.core.execute("u", _act(reviewer="claude"))
    assert rec.dispatches[0]["agent"] == "claude"
    # 마커 코멘트에 라우팅 리뷰어 기록 — 판정 인정(_requested_reviewers) 근거
    assert any("→ claude" in c["body"] for c in rec.commented if c.get("author") == "probe")


def test_execute는_reviewer_없으면_레거시_env로_간다(monkeypatch):
    monkeypatch.setenv("TT_REVIEW_AGENT", "fallback@t")
    card = {"id": "M4JTJ970-WS3C", "state": "review", "version": 3, "comments": []}
    rec = Rec(card)
    monkeypatch.setattr(probe.core, "api", rec)
    probe.core.execute("u", _act())
    assert rec.dispatches[0]["agent"] == "fallback@t"


def test_execute는_레거시_env가_구현가족이면_대체한다(monkeypatch):
    # 자기 리뷰 방지 원칙 우선 — env(codex)도 codex* 구현 카드엔 못 준다
    monkeypatch.setenv("TT_REVIEW_AGENT", "codex")
    card = {"id": "M4JTJ970-WS3C", "state": "review", "version": 3, "comments": [],
            "assignee": "codex-labs"}
    rec = Rec(card)
    monkeypatch.setattr(probe.core, "api", rec)
    probe.core.execute("u", _act())
    assert rec.dispatches[0]["agent"] == "kanban-adapter"


def test_execute는_미표기_카드의_레거시_env를_그대로_쓴다(monkeypatch):
    monkeypatch.setenv("TT_REVIEW_AGENT", "fallback@t")
    card = {"id": "M4JTJ970-WS3C", "state": "review", "version": 3, "comments": [],
            "assignee": ""}
    rec = Rec(card)
    monkeypatch.setattr(probe.core, "api", rec)
    probe.core.execute("u", _act())
    assert rec.dispatches[0]["agent"] == "fallback@t"
