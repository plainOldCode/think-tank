"""카운터파트 리뷰어 라우팅 (M4JTJ970-WS3C) — docs/review-gate.md 룰셋.

리뷰는 작성 에이전트의 카운터파트(상대편)가 맡는다 — 자기 작업 자기 리뷰 방지.
- codex→claude, hermes/opencode/claude→codex (기본 표, 가족 prefix 정규화)
- 미표기·미매칭→codex 기본, codex* 가족 구현→claude (자기 리뷰 방지 우선)
- 자기 리뷰 방지 가드는 고정 가족 어휘+어댑터 별칭(kanban-adapter≡hermes)+본인
  신원 — TT_REVIEW_MAP 확장으로 우회 불가
- 미가용 폴백: claude→kanban-adapter(hermes 어댑터)→codex, 구현 가족 제외
- 수령 인계: probe가 대신 claim-review — 거부(409)면 다음 후보, 전원 거부·활성
  점유면 마커 없이 다음 사이클 재시도
"""
import urllib.error

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
    for impl in ("codex", "codex@host", "claude", "hermes@host", "kanban-adapter",
                 "opencode-tp13", "", "qw-flash"):
        routed = pick_reviewer(impl, agents, set(), RMAP)
        assert routed != impl
        assert agent_family(routed) != agent_family(impl), impl


# --- decide 배선: 양 경로 모두 카운터파트/가족 정책 (리뷰 R1) ---

def _i(**kw):
    base = {"id": "M4JTJ970-WS3C", "state": "review", "archived": 0, "version": 3,
            "assignee": "codex", "reviewer": "", "lease_by": "", "lease_expires": None,
            "labels": [], "dispatches": 1, "execution_attempt": 1,
            "work_contract": {"version": "v2"}, "comments": [],
            "updated_at": "2026-10-10T09:00:00+0900", "title": "M4JTJ970-WS3C"}
    base.update(kw)
    return base


def _snap(issues, prs=(), agents=()):
    return {"auto": True, "now": NOW, "agents": list(agents),
            "issues": list(issues), "prs": list(prs)}


def _pr(number=9, sha="b" * 40):
    return [{"number": number, "repo": "plainOldCode/think-tank",
             "checks": [{"state": "SUCCESS"}],
             "branch": "tt/M4JTJ970-WS3C-x", "title": "", "head_sha": sha}]


def _claim(acts):
    return [a for a in acts if a["action"] == "review-claim"]


def _req(acts):
    return [a for a in acts if a["action"] == "review-request"]


@pytest.fixture(autouse=True)
def gate_on(monkeypatch):
    monkeypatch.setenv("TT_REVIEW_AGENT", "kanban-adapter")  # 게이트 on — env는 레거시 폴백


def test_decide는_유휴_중_카운터파트를_먼저_수령시킨다():
    # 리뷰 R1 — 등록 순서와 무관하게 카운터파트가 1순위(미점유 실제 카드)
    agents = _agents("codex", "opencode-tp13", "claude")
    acts = dispatchd.decide(_snap([_i(assignee="codex")], _pr(), agents))
    claims = _claim(acts)
    assert len(claims) == 1 and claims[0]["agent"] == "claude"
    assert not _req(acts) and not [a for a in acts if a["action"] == "merge"]


def test_decide는_교차_수령에서도_구현가족을_제외한다():
    # 리뷰 R1 — codex@host 구현 카드에 codex(정확명 다름)가 수령하는 우회 차단
    acts = dispatchd.decide(_snap([_i(assignee="codex@host")], _pr(),
                                  _agents("codex", "claude")))
    assert len(_claim(acts)) == 1 and _claim(acts)[0]["agent"] == "claude"


@pytest.mark.parametrize("impl", ["hermes@host", "opencode-tp13", "claude", ""])
def test_decide는_hermes_opencode_claude_미표기_구현을_codex로_수령시킨다(impl):
    # 검증기준 1·2 — 미표기 포함 codex 기본
    acts = dispatchd.decide(_snap([_i(assignee=impl)], _pr(),
                                  _agents("codex", "claude", "kanban-adapter")))
    assert len(_claim(acts)) == 1 and _claim(acts)[0]["agent"] == "codex"


def test_decide는_codex가족_변형도_claude로_라우팅한다():
    for impl in ("codex@host", "codex-labs"):
        acts = dispatchd.decide(_snap([_i(assignee=impl)], _pr(),
                                      _agents("codex", "opencode-tp13", "claude")))
        assert len(_claim(acts)) == 1 and _claim(acts)[0]["agent"] == "claude"


def test_decide는_qw_flash를_codex_기본으로_수령시킨다():
    acts = dispatchd.decide(_snap([_i(assignee="qw-flash")], _pr(),
                                  _agents("codex", "claude")))
    assert len(_claim(acts)) == 1 and _claim(acts)[0]["agent"] == "codex"


def test_카운터파트_리스_보유시_다른가족_유휴에게_수령시킨다():
    # claude가 타 카드 lease 보유 → 구현 가족 아닌 유휴(opencode)가 수령
    busy_card = _i(id="M4ZZZZZZ-BUSY", state="in_progress", assignee="claude",
                   lease_by="claude", lease_expires="2026-10-10T11:00:00+0900")
    acts = dispatchd.decide(_snap([_i(assignee="codex"), busy_card], _pr(),
                                  _agents("codex", "opencode-tp13", "claude")))
    assert len(_claim(acts)) == 1 and _claim(acts)[0]["agent"] == "opencode-tp13"


def test_전원_미가용이면_reviewer_키_없이_요청한다():
    # 수령 가능한 에이전트가 없으면 review-request로 — 레거시 폴백은 execute가 해석
    acts = dispatchd.decide(_snap([_i(assignee="codex")], _pr(), []))
    reqs = _req(acts)
    assert len(reqs) == 1
    assert "reviewer" not in reqs[0]
    assert "kanban-adapter" in reqs[0]["reason"]
    assert not _claim(acts)


def test_유휴_에이전트가_구현가족뿐이면_요청으로_넘어간다():
    # codex 구현 + 등록 에이전트가 codex뿐 → 자기 리뷰 방지로 수령 없음 → 요청 경로
    acts = dispatchd.decide(_snap([_i(assignee="codex")], _pr(), _agents("codex")))
    assert not _claim(acts)
    assert len(_req(acts)) == 1
    assert "reviewer" not in _req(acts)[0]  # codex는 후보에서 제외됐다


def test_decide는_활성_점유_카드에_요청을_만들지_않는다():
    # 리뷰 R3 — tt-reviewer 활성 점유: 요청·마커 없음, lease 만료 후 카운터파트가 자동 수령
    occupied = dict(_i(assignee="codex"), reviewer="tt-reviewer",
                    lease_by="tt-reviewer", lease_expires="2026-10-10T11:00:00+0900")
    acts = dispatchd.decide(_snap([occupied], _pr(), _agents("claude", "codex")))
    assert not _req(acts) and not _claim(acts)
    expired = dict(occupied, lease_expires="2026-10-10T09:00:00+0900")
    acts = dispatchd.decide(_snap([expired], _pr(), _agents("claude", "codex")))
    assert not _req(acts)
    assert len(_claim(acts)) == 1 and _claim(acts)[0]["agent"] == "claude"


# --- execute 배선: 동기 수령 인계, 409 폴백, 레거시 가드 ---

def _api_double(card, claim_fail=(), fail_code=409):
    """api 더블 — GET 카드, claim-review/기록. claim_fail 에이전트는 HTTPError."""
    calls = {"claim": [], "dispatch": [], "comments": []}

    def fake(url, path, method="GET", body=None):
        if method == "GET":
            return dict(card)
        if path.endswith("/claim-review") and method == "POST":
            calls["claim"].append(body)
            if body and body.get("agent") in claim_fail:
                raise urllib.error.HTTPError(path, fail_code, "refused", None, None)
            return {"id": card.get("id", "X")}
        if path.endswith("/dispatch") and method == "POST":
            calls["dispatch"].append(body)
            return {"ok": True}
        calls["comments"].append(body)
        return {"ok": True}

    return fake, calls


def _act(**kw):
    return {"agent": "probe", "issue": "M4JTJ970-WS3C", "action": "review-request",
            "pr": 9, "repo": "plainOldCode/think-tank", "branch": "tt/x",
            "head_sha": "b" * 40, "marker": f"[review-req #9/{SHA8}]", "reason": "r", **kw}


def test_execute는_수령_확인_후_마커를_남긴다(monkeypatch):
    card = {"id": "M4JTJ970-WS3C", "state": "review", "version": 3, "comments": []}
    fake, calls = _api_double(card)
    monkeypatch.setattr(probe.core, "api", fake)
    probe.core.execute("u", _act(reviewer="claude"))
    assert calls["claim"] == [{"agent": "claude"}]  # probe가 점유 표기
    assert calls["dispatch"][0]["agent"] == "claude"
    assert any(f"{_act()['marker']} PR#9 CI green" in (c.get("body") or "")
               and "→ claude" in (c.get("body") or "") for c in calls["comments"])


def test_execute는_수령_거부시_다음_후보로_폴백한다(monkeypatch):
    # claude 거부 → 폴백 체인의 kanban-adapter로 성공(R3)
    card = {"id": "M4JTJ970-WS3C", "state": "review", "version": 3, "comments": []}
    fake, calls = _api_double(card, claim_fail={"claude"})
    monkeypatch.setattr(probe.core, "api", fake)
    probe.core.execute("u", _act(reviewer="claude"))
    assert [c["agent"] for c in calls["claim"]] == ["claude", "kanban-adapter"]
    assert calls["dispatch"][0]["agent"] == "kanban-adapter"
    assert any("→ kanban-adapter" in (c.get("body") or "") for c in calls["comments"])


def test_execute는_전원_거부시_마커없이_건너뛴다(monkeypatch):
    # 리뷰 R3 — 마커가 남으면 같은 head 재시도가 영구 차단된다
    card = {"id": "M4JTJ970-WS3C", "state": "review", "version": 3, "comments": []}
    fake, calls = _api_double(card, claim_fail={"claude", "kanban-adapter", "codex"})
    monkeypatch.setattr(probe.core, "api", fake)
    probe.core.execute("u", _act(reviewer="claude"))
    assert not calls["dispatch"]
    assert not any(_act()["marker"] in (c.get("body") or "") for c in calls["comments"])
    assert any("[review-claim-fail #9/" in (c.get("body") or "") for c in calls["comments"])


def test_execute는_거부_공지를_head당_한번만_남긴다(monkeypatch):
    card = {"id": "M4JTJ970-WS3C", "state": "review", "version": 3,
            "comments": [{"author": "probe",
                          "body": f"[review-claim-fail #9/{SHA8}] 리뷰어 수령 거부(HTTP 409) — "
                                  "다음 사이클 재시도"}]}
    fake, calls = _api_double(card, claim_fail={"claude", "kanban-adapter", "codex"})
    monkeypatch.setattr(probe.core, "api", fake)
    probe.core.execute("u", _act(reviewer="claude"))
    assert not [c for c in calls["comments"] if "review-claim-fail" in (c.get("body") or "")]


def test_execute는_reviewer_없으면_레거시_env로_간다(monkeypatch):
    monkeypatch.setenv("TT_REVIEW_AGENT", "fallback@t")
    card = {"id": "M4JTJ970-WS3C", "state": "review", "version": 3, "comments": []}
    fake, calls = _api_double(card)
    monkeypatch.setattr(probe.core, "api", fake)
    probe.core.execute("u", _act())
    assert calls["dispatch"][0]["agent"] == "fallback@t"


def test_execute는_레거시_env가_구현가족이면_보내지_않는다(monkeypatch):
    # 자기 리뷰 방지 원칙 우선 — env(codex)도 codex* 구현 카드엔 못 준다.
    # decide가 전원 미가용으로 라우팅 없이 넘긴 뒤 레거시마저 가드되면 발송 없이 재시도.
    monkeypatch.setenv("TT_REVIEW_AGENT", "codex")
    card = {"id": "M4JTJ970-WS3C", "state": "review", "version": 3, "comments": [],
            "assignee": "codex-labs"}
    fake, calls = _api_double(card)
    monkeypatch.setattr(probe.core, "api", fake)
    probe.core.execute("u", _act())
    assert not calls["dispatch"] and not calls["claim"]
    assert any("[review-claim-fail #9/" in (c.get("body") or "") for c in calls["comments"])


def test_execute는_레거시가_작성자_본인이면_보내지_않는다(monkeypatch):
    # 리뷰 R2 — kanban-adapter 구현 카드 + env 기본(kanban-adapter): 자기 리뷰 금지
    monkeypatch.delenv("TT_REVIEW_AGENT", raising=False)
    card = {"id": "M4JTJ970-WS3C", "state": "review", "version": 3, "comments": [],
            "assignee": "kanban-adapter"}
    fake, calls = _api_double(card)
    monkeypatch.setattr(probe.core, "api", fake)
    probe.core.execute("u", _act())
    assert not calls["dispatch"] and not calls["claim"]
    assert any("[review-claim-fail #9/" in (c.get("body") or "") for c in calls["comments"])


def test_execute는_미표기_카드의_레거시_env를_그대로_쓴다(monkeypatch):
    monkeypatch.setenv("TT_REVIEW_AGENT", "fallback@t")
    card = {"id": "M4JTJ970-WS3C", "state": "review", "version": 3, "comments": [],
            "assignee": ""}
    fake, calls = _api_double(card)
    monkeypatch.setattr(probe.core, "api", fake)
    probe.core.execute("u", _act())
    assert calls["dispatch"][0]["agent"] == "fallback@t"


def test_execute_review_claim은_수령_거부시_사이클을_죽이지_않는다(monkeypatch):
    # 교차 수령 경로도 409에서 예외 누출 없이 기록만 남긴다(R3)
    card = {"id": "M4JTJ970-WS3C", "state": "review", "version": 3, "comments": []}
    fake, calls = _api_double(card, claim_fail={"a2@t"})
    monkeypatch.setattr(probe.core, "api", fake)
    marker = f"[review-req #9/{SHA8}]"
    probe.core.execute("u", {"action": "review-claim", "agent": "a2@t",
                             "issue": "M4JTJ970-WS3C", "pr": 9,
                             "repo": "plainOldCode/think-tank", "head_sha": "b" * 40,
                             "marker": marker})
    assert not calls["dispatch"]
    assert not any(marker in (c.get("body") or "") for c in calls["comments"])
    assert any("[review-claim-fail #9/" in (c.get("body") or "") for c in calls["comments"])
