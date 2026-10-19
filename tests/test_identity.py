"""신원·별칭 해석 통합 — 3QMV(M568PJ79-3QMV)·W0DF·DX88(M5283J3A-DX88)·R7Y4(M53PBDC0-R7Y4)·XKRY(M58SPRZ7-XKRY).

U6(M5A4HZQB-AT3N)에서 5개 카드 파일을 하나로 모았다(중복은 매개변수화로 합침).
단일 소스: server/identity.py(해석·가족) + ontology/catalog.py(엔티티 게이트).
동작 불변 — 진입점별 규약(서버=별칭까지, probe=가족 폴백 포함)을 그대로 잠근다.
"""
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

import identity
import service

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "server"))


def _agents(*specs):
    return [{"name": n, "aliases": list(a)} for n, a in specs]


AGENTS = _agents(("codex", ["cody", "codex-labs"]),
                 ("hermes-m2max", ["hermes"]),
                 ("kanban-adapter", []),
                 ("qw-flash", []))


# ---------- 3QMV: 해석 규칙 단일 소스 ----------

def test_해석_표_등록명_별칭_가족_미등록():
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
    assert core.agent_family("skshim@tp-13") == "opencode"


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


# ---------- W0DF: 정의 일치(비-신원이지만 원 파일에서 동행) ----------

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


def test_js는_활성_상태를_하드코딩하지_않는다():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    js = open(os.path.join(root, "server", "static", "js", "tt-run.js")).read()
    assert '"queued","running","stalled"' not in js.replace(" ", "")


# ---------- DX88: 수령자 이름 정규화 ----------
# 배경: assignee 고유값 20+ 대 등록 에이전트 10개 — 미등록 표기 저장 시 probe 리뷰
# dispatch가 404로 낙하한다(에픽 #1 직접 원인). Z1HP 별칭 → 등록명 치환.

def _agent(client, name, aliases=None, **kw):
    payload = {"name": name, "base_url": "http://x/hook", "receive_mode": "hook", **kw}
    if aliases is not None:
        payload["aliases"] = aliases
    return client.post("/agents", json=payload)


def _todo(client, **kw):
    kw.setdefault("acceptance", "완료 기준: 테스트 통과")
    return client.post("/issues", json={"title": "DX88", **kw}).json()


def _claim(client, iid, name):
    return client.post(f"/issues/{iid}/claim", json={"agent": name})


def _pull(client, name):
    return client.post("/pull", json={"agent": name})


def _patch(client, iid, name):
    return client.patch(f"/issues/{iid}", json={"assignee": name})


def _claim_review(client, iid, name):
    return client.post(f"/issues/{iid}/claim-review", json={"agent": name})


_ROUTES = {
    "claim": lambda c, iid, n: _claim(c, iid, n),
    "pull": lambda c, iid, n: _pull(c, n),
    "patch": lambda c, iid, n: _patch(c, iid, n),
    "claim-review": lambda c, iid, n: _claim_review(c, iid, n),
}


@pytest.mark.parametrize("route", ["claim", "pull", "patch", "claim-review"],
                         ids=["claim", "pull", "patch", "claim-review"])
def test_별칭_정규화는_모든_수령_경로에서_적용된다(client, route):
    """DX88 — claim·pull·patch assignee·claim-review에서 별칭이 등록명으로 저장된다."""
    if route == "claim-review":
        _agent(client, "worker")
        _agent(client, "rev", aliases=["rev@mini"])
        i = _todo(client)
        _claim(client, i["id"], "worker")
        client.patch(f"/issues/{i['id']}", json={"state": "review"})
        r = _ROUTES[route](client, i["id"], "rev@mini")
        assert r.status_code == 200, r.text
        assert r.json()["reviewer"] == "rev"
        return
    _agent(client, "codex", aliases=["codex@m2pro"])
    if route == "pull":
        i = _todo(client, priority=1)
    else:
        i = _todo(client)
    r = _ROUTES[route](client, i["id"], "codex@m2pro")
    assert r.status_code == 200, r.text
    assert r.json().get("assignee") == "codex"


def test_등록명_정확_일치가_별칭보다_우선한다(client):
    _agent(client, "codex", aliases=["alias-x"])
    i = _todo(client)
    r = _claim(client, i["id"], "codex")
    assert r.status_code == 200 and r.json()["assignee"] == "codex"


def test_공백이_붙은_이름은_정리되어_저장된다(client):
    _agent(client, "codex")
    i = _todo(client)
    r = _claim(client, i["id"], "  codex  ")
    assert r.status_code == 200 and r.json()["assignee"] == "codex"


@pytest.mark.parametrize("route", ["claim", "pull", "patch"],
                         ids=["claim", "pull", "patch"])
@pytest.mark.parametrize("junk", ["", "   ", "1", "12345"],
                         ids=["blank", "spaces", "one-digit", "digits-only"])
def test_정크_이름은_모든_수령_경로에서_거부된다(client, route, junk):
    """DX88 — 빈 값·숫자만 있는 이름은 422(정규화 전 별칭 우회 차단).
    patch는 정확히 빈 문자열만 '해제'로 해석(200, 공백은 422) — 해제 전용 테스트 참조."""
    i = _todo(client)
    r = _ROUTES[route](client, i["id"], junk)
    if route == "patch" and junk == "":
        assert r.status_code == 200, (route, junk, r.text)
    else:
        assert r.status_code == 422, (route, junk, r.text)


def test_patch_assignee_빈_값은_해제로_허용된다(client):
    """정크 거부와 달리 patch의 빈 문자열은 '해제' 의미 — 수동 해제 경로 보존."""
    _agent(client, "codex")
    i = _todo(client)
    _claim(client, i["id"], "codex")
    r = client.patch(f"/issues/{i['id']}", json={"assignee": ""})
    assert r.status_code == 200 and r.json()["assignee"] == "" and r.json()["lease_by"] == ""


@pytest.mark.parametrize("route", ["claim", "pull", "patch"],
                         ids=["claim", "pull", "patch"])
def test_게이트_활성화_시_미등록_이름은_거부된다(client, monkeypatch, route):
    """DX88 — TT_REQUIRE_KNOWN_AGENT=1이면 미등록 이름 409(policy: assignee_unregistered)."""
    monkeypatch.setenv("TT_REQUIRE_KNOWN_AGENT", "1")
    i = _todo(client, priority=1 if route == "pull" else None)
    r = _ROUTES[route](client, i["id"], "ghost@nowhere")
    assert r.status_code == 409 and "assignee_unregistered" in r.json()["detail"], r.text
    # 등록 후에는 통과(별칭 등록이 곧 정규화 등록)
    _agent(client, "ghost", aliases=["ghost@nowhere"])
    if route == "pull":
        _todo(client, priority=2)
    assert _ROUTES[route](client, i["id"], "ghost@nowhere").status_code == 200


def test_게이트_기본값은_비활성으로_미등록_이름도_수령된다(client):
    """하위 호환 — 게이트 off에서는 현행 동작 유지(등록 유도는 409 메시지로 대체 불가)."""
    i = _todo(client)
    r = _claim(client, i["id"], "skshim@tp-13")
    assert r.status_code == 200 and r.json()["assignee"] == "skshim@tp-13"


def test_재claim_충돌은_정규화된_이름으로_판정한다(client):
    """todo 상태에 assignee 선행 지정 카드 — 별칭 claim은 본인으로 인정(200),
    타 에이전트는 already claimed 409. 정규화 전이면 별칭 claim이 우회 통과한다."""
    _agent(client, "codex", aliases=["codex@m2pro"])
    _agent(client, "other")
    j = _todo(client)
    client.patch(f"/issues/{j['id']}", json={"assignee": "codex"})
    assert _claim(client, j["id"], "codex@m2pro").status_code == 200
    k = _todo(client)
    client.patch(f"/issues/{k['id']}", json={"assignee": "codex"})
    r = _claim(client, k["id"], "other")
    assert r.status_code == 409 and "already claimed by codex" in r.json()["detail"]


def test_lease_한도_판정도_정규화된_이름으로_집계한다(client):
    _agent(client, "w", aliases=["w@t"])
    a = _todo(client)
    b = _todo(client, priority=2)
    assert _claim(client, a["id"], "w").status_code == 200
    # TT_MAX_LEASES 기본 2 — 같은 에이전트(별칭 표기) 두 번째 lease는 성공
    assert _claim(client, b["id"], "w@t").status_code == 200
    c = _todo(client, priority=3)
    r = _claim(client, c["id"], "w")
    assert r.status_code == 409 and "lease limit" in r.json()["detail"]


# ---------- DX88 리뷰 반영 + R7Y4: heartbeat·verify·자기승인 ----------

def test_별칭으로_heartbeat와_ping이_통과한다(client):
    """DX88 — lease_by는 등록명으로 저장되지만 워커는 별칭으로 계속 호출한다."""
    _agent(client, "codex", aliases=["codex@m2pro"])
    i = _todo(client)
    _claim(client, i["id"], "codex@m2pro")
    r = client.post(f"/issues/{i['id']}/lease", json={"agent": "codex@m2pro", "hours": 1})
    assert r.status_code == 200, r.text
    r = client.post(f"/issues/{i['id']}/ping", json={"agent": "codex@m2pro"})
    assert r.status_code == 200, r.text


def test_별칭_verifier로_자기승인이_차단된다(client):
    """DX88 — assignee는 정규명 저장, verifier는 별칭 전달 — 가드가 우회되면 안 된다."""
    _agent(client, "worker", aliases=["worker@mini"])
    i = _todo(client)
    _claim(client, i["id"], "worker@mini")
    client.patch(f"/issues/{i['id']}", json={"state": "review"})
    r = client.post(f"/issues/{i['id']}/verify",
                    json={"verifier": "worker@mini", "evidence": "pytest passed"})
    assert r.status_code == 409 and "cross-review" in r.json()["detail"]


# --- R7Y4 재현: 카드가 비정규 이름으로 수령된 뒤 별칭이 늦게 등록되는 시나리오 ---

@pytest.fixture()
def r7y4_client(tmp_path, monkeypatch):
    """v2 계약 — 제출은 review 상태 전이(보고 필수). 자기승인·자가수령 가드는 v1·v2
    공통이지만 테스트 재현은 v2 제출 경로가 단순하다."""
    import app as appmod
    from fastapi.testclient import TestClient
    monkeypatch.setenv("TT_REQUIRE_REPORT", "1")
    monkeypatch.setenv("TT_CONTRACT_VERSION", "2")
    m = __import__("importlib").reload(appmod)
    return TestClient(m.create_app(str(tmp_path / "tt.db")))


@pytest.fixture()
def r7y4_iid(r7y4_client):
    return r7y4_client.post("/issues", json={"title": "자기승인 우회 재현", "type": "task",
                                             "acceptance": "완료 기준: 테스트 통과"}).json()["id"]


def _r7y4_to_review(client, iid):
    issue = client.get(f"/issues/{iid}").json()
    rep = {"contract_version": issue["work_contract"]["version"],
           "attempt": issue["execution_attempt"], "method": "planned",
           "result": "passed",
           "design": {"criteria": "c", "verification": "v"},
           "implementation": {"summary": "s", "commands": "cc"},
           "verification": {"commands": "ccc", "evidence": "1 passed"}}
    r = client.patch(f"/issues/{iid}", json={"state": "done", "completion_report": rep})
    assert r.status_code == 200, r.text


@pytest.fixture()
def r7y4_late_alias(r7y4_client, r7y4_iid):
    """카드를 비정규화 이름 'worker'로 claim 후, 별칭 'worker'→'worker@t'를 나중에 등록."""
    r = r7y4_client.post(f"/issues/{r7y4_iid}/claim", json={"agent": "worker", "hours": 2})
    assert r.status_code == 200, r.text
    r = r7y4_client.post("/agents", json={"name": "worker@t", "base_url": "http://127.0.0.1:1",
                                          "aliases": ["worker"]})
    assert r.status_code in (200, 201), r.text


@pytest.fixture()
def r7y4_submitted(r7y4_client, r7y4_iid, r7y4_late_alias):
    _r7y4_to_review(r7y4_client, r7y4_iid)


@pytest.mark.parametrize("route", ["verify", "claim-review"], ids=["verify", "claim-review"])
def test_비정규_저장값과_늦은_별칭에서도_가드는_작동한다(r7y4_client, r7y4_iid, r7y4_submitted, route):
    """R7Y4 — 저장값이 비정규화('worker')인 채로 늦게 등록된 별칭의 등록명('worker@t')
    호출: verify는 자기승인 409, claim-review는 본인 작업 수령 409."""
    payload = {"verifier": "worker@t"} if route == "verify" else {"agent": "worker@t"}
    r = r7y4_client.post(f"/issues/{r7y4_iid}/{route}", json=payload)
    assert r.status_code == 409, r.text
    assert "cross-review" in r.json()["detail"]


def test_비정규_저장값의_정당보유자_heartbeat는_수용된다(r7y4_client, r7y4_iid, r7y4_late_alias):
    """R7Y4 — 제출 전 lease는 남아 있으므로 등록명 heartbeat·ping이 정당 보유자로 통과."""
    r = r7y4_client.post(f"/issues/{r7y4_iid}/lease", json={"agent": "worker@t", "hours": 2})
    assert r.status_code == 200, r.text
    r = r7y4_client.post(f"/issues/{r7y4_iid}/ping", json={"agent": "worker@t"})
    assert r.status_code == 200, r.text


# ---------- 마이그레이션(DX88 후속 + R7Y4 reviewer/nominee) ----------

def test_마이그레이션이_별칭_assignee를_등록명으로_치환한다(client, tmp_path):
    from migrations import migrate_assignees
    _agent(client, "codex", aliases=["codex@m2pro"])
    i = _todo(client)
    con = sqlite3.connect(tmp_path / "tt.db")
    con.row_factory = sqlite3.Row
    con.execute("UPDATE issues SET assignee='codex@m2pro' WHERE id=?", (i["id"],))
    con.commit()
    stats = migrate_assignees(con, apply=True)
    assert stats["assignee_normalized"] == 1
    assert client.get(f"/issues/{i['id']}").json()["assignee"] == "codex"


def test_마이그레이션이_숫자_정크_assignee를_해제한다(client, tmp_path):
    from migrations import migrate_assignees
    i = _todo(client)
    con = sqlite3.connect(tmp_path / "tt.db")
    con.row_factory = sqlite3.Row
    con.execute("UPDATE issues SET assignee='1' WHERE id=?", (i["id"],))
    con.commit()
    stats = migrate_assignees(con, apply=True)
    assert stats["assignee_cleared"] == 1
    assert client.get(f"/issues/{i['id']}").json()["assignee"] == ""


def test_마이그레이션이_진행_카드의_정크_점유를_재큐한다(client, tmp_path):
    """in_progress + 정크 점유는 회수 불가능한 좀비 — todo로 되돌려 재수령 가능하게."""
    from migrations import migrate_assignees
    i = _todo(client)
    con = sqlite3.connect(tmp_path / "tt.db")
    con.row_factory = sqlite3.Row
    con.execute("UPDATE issues SET state='in_progress', assignee='1', lease_by='1', "
                "lease_expires='2099-01-01T00:00:00+0900' WHERE id=?", (i["id"],))
    con.commit()
    stats = migrate_assignees(con, apply=True)
    row = client.get(f"/issues/{i['id']}").json()
    assert row["state"] == "todo" and row["assignee"] == ""
    assert row["lease_by"] == "" and row["lease_expires"] is None
    # 재수령 가능 — 좀비가 아니라 큐로 복귀
    assert _claim(client, i["id"], "w").status_code == 200
    assert stats["assignee_cleared"] == 1


def test_마이그레이션은_미등록_호스트_표기를_보존한다(client, tmp_path):
    """해석 불가한 이름은 감사 가치 보존 — 게이트 활성 후 새 수령만 차단된다."""
    from migrations import migrate_assignees
    i = _todo(client)
    con = sqlite3.connect(tmp_path / "tt.db")
    con.row_factory = sqlite3.Row
    con.execute("UPDATE issues SET assignee='legacy@old-host' WHERE id=?", (i["id"],))
    con.commit()
    migrate_assignees(con, apply=True)
    assert client.get(f"/issues/{i['id']}").json()["assignee"] == "legacy@old-host"


def test_마이그레이션은_멱등이다(client, tmp_path):
    from migrations import migrate_assignees
    _agent(client, "codex", aliases=["codex@m2pro"])
    a, b = _todo(client), _todo(client)
    con = sqlite3.connect(tmp_path / "tt.db")
    con.row_factory = sqlite3.Row
    con.execute("UPDATE issues SET assignee='codex@m2pro' WHERE id=?", (a["id"],))
    con.execute("UPDATE issues SET assignee='1', state='in_progress', lease_by='1' WHERE id=?", (b["id"],))
    con.commit()
    s1 = migrate_assignees(con, apply=True)
    s2 = migrate_assignees(con, apply=True)
    assert s1["assignee_normalized"] == 1 and s1["assignee_cleared"] == 1
    # R7Y4: reviewer/nominee 칸 추가 — 멱등 2회차는 전부 0
    assert s2 == {"assignee_normalized": 0, "assignee_cleared": 0,
                  "lease_by_normalized": 0, "lease_by_cleared": 0,
                  "reviewer_normalized": 0, "reviewer_cleared": 0,
                  "nominee_normalized": 0, "nominee_cleared": 0}


def test_마이그레이션_스크립트가_dry_run과_apply를_구분한다(client, tmp_path):
    from pathlib import Path
    db = tmp_path / "tt.db"
    i = _todo(client)
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    con.execute("UPDATE issues SET assignee='1' WHERE id=?", (i["id"],))
    con.commit()
    con.close()
    script = str(Path(__file__).resolve().parent.parent / "scripts" / "migrate_assignees.py")
    dry = subprocess.run([sys.executable, script, str(db)], capture_output=True, text=True)
    assert dry.returncode == 0 and '"assignee_cleared": 1' in dry.stdout.replace("'", '"')
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    assert con.execute("SELECT assignee FROM issues WHERE id=?", (i["id"],)).fetchone()["assignee"] == "1"
    app = subprocess.run([sys.executable, script, str(db), "--apply"], capture_output=True, text=True)
    assert app.returncode == 0
    con.row_factory = sqlite3.Row
    assert con.execute("SELECT assignee FROM issues WHERE id=?", (i["id"],)).fetchone()["assignee"] == ""


def test_마이그레이션에_reviewer_nominee_포함(r7y4_client, r7y4_iid, tmp_path):
    """R7Y4 — 마이그레이션이 reviewer·nominee 칸도 정규화한다."""
    from migrations import migrate_assignees
    r7y4_client.post(f"/issues/{r7y4_iid}/claim", json={"agent": "worker", "hours": 2})
    r7y4_client.post("/agents", json={"name": "worker@t", "base_url": "http://127.0.0.1:1",
                                      "aliases": ["worker"]})
    _r7y4_to_review(r7y4_client, r7y4_iid)
    r = r7y4_client.post(f"/issues/{r7y4_iid}/claim-review", json={"agent": "other"})
    assert r.status_code == 200, r.text
    con = sqlite3.connect(tmp_path / "tt.db")
    con.row_factory = sqlite3.Row
    con.execute("UPDATE issues SET reviewer='worker', nominee='worker' WHERE id=?",
                (r7y4_iid,))
    con.commit()
    stats = migrate_assignees(con, apply=True)
    assert stats["reviewer_normalized"] == 1 and stats["nominee_normalized"] == 1
    row = con.execute("SELECT reviewer, nominee FROM issues WHERE id=?",
                      (r7y4_iid,)).fetchone()
    assert row["reviewer"] == "worker@t" and row["nominee"] == "worker@t"


# ---------- XKRY: 엔티티 정본 카탈로그 ----------

from ontology import catalog  # noqa: E402

XKRY_AGENTS = [
    {"name": "hermes", "aliases": ["kanban-adapter"]},
    {"name": "opencode-tp13", "aliases": []},
    {"name": "cursor-review", "aliases": []},
]


@pytest.fixture(autouse=True)
def _reset_unknown():
    catalog.reset_unknown()
    yield
    catalog.reset_unknown()


def test_agent_exact_and_alias():
    assert catalog.resolve("agent", "hermes", agents=XKRY_AGENTS) == "hermes"
    assert catalog.resolve("agent", "kanban-adapter", agents=XKRY_AGENTS) == "hermes"


def test_agent_unregistered_records_unknown_id_only():
    assert catalog.resolve("agent", "ghost-agent", agents=XKRY_AGENTS) is None
    rec = catalog.unknown_log()[-1]
    assert set(rec) == {"kind", "raw_id", "n"}  # id만 — 본문 필드 없음(TV Q4)
    assert rec["kind"] == "agent" and rec["raw_id"] == "ghost-agent"
    assert catalog.unknown_counts()["agent"] == 1


def test_agent_case_and_suffix_variants():
    # identity 계약(정확명 대소문자 구분) 그대로 — 카탈로그가 정규화로 계약을 바꾸지 않는다
    assert catalog.resolve("agent", "Hermes", agents=XKRY_AGENTS) is None
    assert catalog.resolve("agent", "hermes@mini", agents=XKRY_AGENTS) is None
    assert catalog.unknown_counts()["agent"] == 2
    # 가족 폴백은 옵션(probe 스냅샷 소비 전용) — 켜면 접미 변형도 정본화
    assert catalog.resolve("agent", "hermes@mini", agents=XKRY_AGENTS,
                           family_fallback=True) == "hermes"


def test_reviewer_strict_no_family_fallback():
    assert catalog.resolve("reviewer", "cursor-review", agents=XKRY_AGENTS) == "cursor-review"
    assert catalog.resolve("reviewer", "cursor", agents=XKRY_AGENTS) is None  # 가족 폴백 없음


def test_card_canonical():
    assert catalog.resolve("card", "M58SPRZ7-XKRY") == "M58SPRZ7-XKRY"
    assert catalog.resolve("card", "  m58sprz7-xkry ") == "M58SPRZ7-XKRY"


def test_card_invalid_is_unknown_not_promoted():
    assert catalog.resolve("card", "not-a-card-id!!") is None
    assert catalog.resolve("card", "") is None
    assert catalog.unknown_counts()["card"] == 2


def test_pr_ref_and_url_canonical():
    assert catalog.resolve("pr", "plainOldCode/think-tank#171") == "plainOldCode/think-tank#171"
    assert catalog.resolve("pr", "https://github.com/plainOldCode/think-tank/pull/171") \
        == "plainOldCode/think-tank#171"


def test_pr_repo_case_preserved():
    # 조인이 정확 문자열 매치 — 카탈로그가 케이스를 바꾸면 계약이 깨진다
    assert catalog.resolve("pr", "PlainOldCode/Think-Tank#9") == "PlainOldCode/Think-Tank#9"


def test_pr_invalid_is_unknown():
    assert catalog.resolve("pr", "think-tank#9") is None  # owner 없음
    assert catalog.resolve("pr", "o/r/p#9") is None
    assert catalog.resolve("pr", "plain text") is None
    assert catalog.unknown_counts()["pr"] == 3


def test_unknown_kind_raises():
    with pytest.raises(ValueError):
        catalog.resolve("planet", "earth")


def test_dispatch_unregistered_target_404_via_catalog(client):
    r = client.post("/issues", json={"title": "t", "acceptance": "c"})
    iid = r.json()["id"]
    resp = client.post(f"/issues/{iid}/dispatch", json={"agent": "ghost", "message": "x"})
    assert resp.status_code == 404  # 기존 계약 유지(test_api.py:749 회귀)


def test_dispatch_alias_target_resolves_via_catalog(client):
    assert client.post("/agents", json={"name": "alias-src", "aliases": ["alias-registered"],
                                        "base_url": "http://127.0.0.1:1/hook",
                                        "receive_mode": "hook"}).status_code == 201
    iid = client.post("/issues", json={"title": "t", "acceptance": "c"}).json()["id"]
    resp = client.post(f"/issues/{iid}/dispatch", json={"agent": "alias-registered", "message": "x"})
    assert resp.status_code == 201, resp.text  # 별칭 → 카탈로그 경로로 정본화


def test_unknown_entity_log_never_holds_bodies():
    catalog.resolve("card", "M4ABCDEF-GH12 some very long prose body that is not an id")
    for rec in catalog.unknown_log():
        assert "body" not in rec and "raw" not in rec
        assert len(rec["raw_id"]) <= 120
