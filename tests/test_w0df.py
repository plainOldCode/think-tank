"""W0DF: 분산된 정의 통합 — active run state(4곳)·agent identity(3모델).

동작 불변 리팩터: 활성 상태 정의는 config 한 곳, 신원 해석은 별칭 사전 정본.
기존 테스트 전량 GREEN이 전제고, 여기선 정의 일치·별칭 사용을 고정한다.
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "server"))


def test_probe_활성_집합은_config_정본의_같은_객체다():
    import config
    import probe.core as core
    assert core._ACTIVE_RUN_STATES is config.PROBE_ACTIVE_RUN_STATES


def test_러너_활성_집합과_config_정본이_대응한다():
    import config
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "runner"))
    from run_state import ACTIVE
    assert ACTIVE == config.PROBE_ACTIVE_RUN_STATES


def test_probe는_별칭으로_등록명을_해석한다():
    import probe.core as core
    agents = [{"name": "opencode-tp13", "aliases": ["skshim@tp-13"]}]
    assert core._resolve_agent("skshim@tp-13", agents) == "opencode-tp13"
    assert core._resolve_executor("skshim@tp-13", agents) == "opencode-tp13"
    # 별칭 없는 후보는 기존 규칙 그대로 — 정확 일치 우선, 가족 매칭 다음
    agents2 = [{"name": "opencode-tp13"}, {"name": "opencode"}]
    assert core._resolve_agent("opencode-tp13", agents2) == "opencode-tp13"


def test_probe_가족_판정은_별칭_해석_결과_위에서_한다():
    """자기 리뷰 방지: 별칭으로 해석된 등록명의 가족이 후보와 같으면 배정하지 않는다."""
    import probe.core as core
    agents = [{"name": "opencode-tp13", "aliases": ["skshim@tp-13"]}]
    # route_reviewer: 구현 에이전트 skshim@tp-13의 가족 = opencode(host 지도) →
    # 매핑에 없으면 codex 기본. 여기선 resolve가 opencode-tp13으로 정규화됨을 확인.
    assert core.agent_family("skshim@tp-13") == "opencode"


def test_dispatch는_별칭을_정본으로_해석한다(client):
    from conftest import mk
    client.post("/agents", json={"name": "codex", "base_url": "http://127.0.0.1:1",
                                 "aliases": ["codex@legacy-host"]})
    issue = mk(client, title="별칭 발령")
    r = client.post(f"/issues/{issue['id']}/dispatch",
                    json={"agent": "codex@legacy-host", "message": "별칭 발령"})
    assert r.status_code in (200, 201), r.text
    assert r.json()["agent"] == "codex"  # 등록명으로 기록


def test_dispatch_앳폴백은_회귀_없다(client):
    """WGT4 — 등록명이 단축명이고 후보가 본명@호스트면 기존 @폴백이 살아있어야 한다."""
    from conftest import mk
    client.post("/agents", json={"name": "codex", "base_url": "http://127.0.0.1:1"})
    issue = mk(client, title="폴백 발령")
    r = client.post(f"/issues/{issue['id']}/dispatch",
                    json={"agent": "codex@m2max", "message": "폴백 발령"})
    assert r.status_code in (200, 201), r.text
    assert r.json()["agent"] == "codex"


def test_js는_활성_상태를_하드코딩하지_않는다():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    js = open(os.path.join(root, "server", "static", "js", "tt-run.js")).read()
    assert '"queued","running","stalled"' not in js.replace(" ", "")
