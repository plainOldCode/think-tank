"""3QMV(M568PJ79-3QMV): 에이전트 신원 해석 단일 소스(server/identity.py).

서버(resolve_agent_identity)·probe(_resolve_agent)·가족 규칙(agent_family)이
각자 규칙을 들고 있던 것을 identity.py 하나로 모은다. 동작 불변이 1차 기준 —
진입점별 기존 규약(서버=별칭까지, probe=가족 폴백 포함)을 표 기반으로 잠근다.
"""
import json
import sqlite3

import identity
import service


def _agents(*specs):
    return [{"name": n, "aliases": list(a)} for n, a in specs]


AGENTS = _agents(("codex", ["cody", "codex-labs"]),
                 ("hermes-m2max", ["hermes"]),
                 ("kanban-adapter", []),
                 ("qw-flash", []))


def test_표_등록명_별칭_가족_미등록():
    # (후보, 기대, 설명)
    table = [
        ("codex", "codex", "정확 일치 우선"),
        ("cody", "codex", "별칭"),
        ("codex-labs", "codex", "별칭 — 가족 경유 자기 리뷰 방지용 매핑 키"),
        ("hermes", "hermes-m2max", "별칭"),
        ("codex@m2max", "codex", "user@host — base 가족 매칭"),
        ("codex-x", "codex", "가족 prefix 매칭"),
        ("qw-flash", "qw-flash", "등록명 — 가족 미지라 가족 폴백은 없지만 정확 일치"),
        ("ghost-x", None, "미등록·가족 미지"),
        ("", None, "빈 후보"),
    ]
    # 서버 규약(family_fallback=False): 정확 일치·별칭까지만 해석
    server_want = {"codex": "codex", "cody": "codex", "codex-labs": "codex",
                   "hermes": "hermes-m2max", "hermes-m2max": "hermes-m2max",
                   "qw-flash": "qw-flash"}
    for cand, want, why in table:
        assert identity.resolve(cand, AGENTS) == want, (cand, why)
        assert identity.resolve(cand, AGENTS, family_fallback=False) == \
            server_want.get(cand), (cand, "서버 규약 — 별칭까지만", why)


def test_가족_규칙_단일_소스():
    assert identity.agent_family("kanban-adapter") == "hermes"
    assert identity.agent_family("cursor-review") == "cursor"
    assert identity.agent_family("skshim@tp-13") == "opencode"
    assert identity.agent_family("codex@m2max") == "codex"
    assert identity.agent_family("qw-flash") is None
    assert identity.parse_host_family_map("tp-13->opencode, bad") == {"tp-13": "opencode"}


def test_probe와_service가_같은_함수를_쓴다(client):
    import probe.core as core

    r = client.post("/agents", json={"name": "codex", "aliases": ["cody"],
                                     "base_url": "http://x"})
    assert r.status_code in (200, 201), r.text
    con = sqlite3.connect(client.app.state.ctx.db_path)
    con.row_factory = sqlite3.Row
    rows = [{"name": x["name"], "aliases": json.loads(x["aliases"] or "[]")}
            for x in con.execute("SELECT name, aliases FROM agents").fetchall()]

    # 등록명·별칭: 서버(probe 스냅샷 동일 규칙)
    assert service.resolve_agent_identity(con, "cody") == "codex"
    assert core._resolve_agent("cody", rows) == "codex"
    # 서버는 가족 폴백 없음 — probe는 있다(기존 규약 불변)
    assert service.resolve_agent_identity(con, "codex-x") is None
    assert core._resolve_agent("codex-x", rows) == "codex"
    con.close()


def test_dispatch_라우트의_본명_at_호스트_폴백은_존치(client):
    """WGT4 — 본명@호스트 → 단축명 폴백은 dispatch 404 무한 실패 수정. 존치 근거:
    probe 스냅샷 규약에 '@' 분할 규칙을 추가하면 probe 해석이 변해 동작 불변이
    깨진다 — 서버 라우트 로컬 규칙으로 남긴다(카드 제안 2)."""
    from routers.agents import _resolve_dispatch_agent

    class P:
        agent = "codex@m2max"

    r = client.post("/agents", json={"name": "codex", "base_url": "http://x"})
    assert r.status_code in (200, 201), r.text  # 테이블 초기화 겸 등록
    con = sqlite3.connect(client.app.state.ctx.db_path)
    con.row_factory = sqlite3.Row
    ag = _resolve_dispatch_agent(con, P())
    con.close()
    assert ag is not None and ag["name"] == "codex"  # 본명@호스트 → 단축명 폴백
