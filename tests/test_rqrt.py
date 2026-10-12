"""RQRT(M4MGV3CM) — 수신 계약: 등록 에이전트 메시지 수신 3모드(hook/watcher/relay)
명시화 + claim 시 검증 + probe 재할당 타이머 모드별 파라미터화."""
import pytest

import importlib

failure_rules = importlib.import_module("failure_rules")
from probe import core as probe_core


# --- AgentIn 수신 계약 선언 ---

def _client(tmp_path):
    from app import create_app
    from fastapi.testclient import TestClient
    return TestClient(create_app(str(tmp_path / "rqrt.db")))


def _todo(client, **kw):
    body = {"title": "RQRT 수신 계약 테스트", "acceptance": "완료 기준: 테스트 통과",
            "state": "todo", **kw}
    return client.post("/issues", json=body).json()


def _agent(client, name, mode=None, interval=None):
    payload = {"name": name, "base_url": "https://agent.example/hook"}
    if mode is not None:
        payload["receive_mode"] = mode
    if interval is not None:
        payload["watcher_interval"] = interval
    return client.post("/agents", json=payload)


def test_등록_에이전트가_수신모드를_선언한다(client):
    r = _agent(client, "hook-agent", mode="hook")
    assert r.status_code == 201 and r.json()["receive_mode"] == "hook"
    w = _agent(client, "watch-agent", mode="watcher", interval=15)
    assert w.status_code == 201 and w.json()["watcher_interval"] == 15
    relayed = _agent(client, "relay-agent", mode="relay")
    assert relayed.status_code == 201


def test_watcher는_주기_명시가_필수다(client):
    r = _agent(client, "no-interval", mode="watcher")
    assert r.status_code == 422
    bad = _agent(client, "bad-mode", mode="poll")
    assert bad.status_code == 422


def test_선언되지_않은_등록_에이전트의_claim은_409다(client):
    _agent(client, "silent-agent")  # receive_mode 미선언
    i = _todo(client)
    r = client.post(f"/issues/{i['id']}/claim", json={"agent": "silent-agent"})
    assert r.status_code == 409
    assert "receive_mode_undeclared" in r.json()["detail"]


def test_선언된_에이전트와_비등록_에이전트는_정상_수령한다(client):
    _agent(client, "declared", mode="hook")
    i1 = _todo(client)
    assert client.post(f"/issues/{i1['id']}/claim",
                       json={"agent": "declared"}).status_code == 200
    # 비등록 세션 = 암묵 relay — 합법 경로(RQRT: 사람 전달)
    i2 = _todo(client)
    assert client.post(f"/issues/{i2['id']}/claim",
                       json={"agent": "ghost@nowhere"}).status_code == 200


def test_마이그레이션은_patch로_선언한다(client):
    """기존 에이전트 — 미선언 409 → PATCH 선언 → 수령 성공."""
    _agent(client, "legacy-agent")
    i = _todo(client)
    assert client.post(f"/issues/{i['id']}/claim",
                       json={"agent": "legacy-agent"}).status_code == 409
    p = client.patch("/agents/legacy-agent", json={"receive_mode": "watcher",
                                                   "watcher_interval": 20})
    assert p.status_code == 200
    i2 = _todo(client)
    assert client.post(f"/issues/{i2['id']}/claim",
                       json={"agent": "legacy-agent"}).status_code == 200


# --- probe 무반응 타이머 모드별 파라미터화 ---

def test_타이머는_모드별로_다르다():
    ags = [{"name": "h", "receive_mode": "hook"},
           {"name": "w", "receive_mode": "watcher", "watcher_interval": 15},
           {"name": "w2", "receive_mode": "watcher", "watcher_interval": 60},
           {"name": "r", "receive_mode": "relay"},
           {"name": "x", "receive_mode": ""}]
    assert probe_core._relay_timer_s("h", ags) == 3600           # hook 1h
    assert probe_core._relay_timer_s("w", ags) == 15 * 3 * 60    # 주기×3
    assert probe_core._relay_timer_s("w2", ags) == 60 * 3 * 60
    assert probe_core._relay_timer_s("r", ags) == 6 * 3600       # relay 6h
    assert probe_core._relay_timer_s("x", ags) == 6 * 3600       # 미선언 = relay 기본
    assert probe_core._relay_timer_s("nobody", ags) == 6 * 3600


# --- 409 코드 매트릭스 행 ---

def test_receive_mode_undeclared가_규칙표에_있다():
    rows = [r for r in failure_rules.rules()
            if r.get("code") == "receive_mode_undeclared"]
    assert rows and "409" in rows[0]["judgment"]


# --- codex 리뷰: 경합·우회 방어 ---

def test_pull도_수신_계약을_거친다(client):
    """pull 우회 차단(R2) — claim과 같은 관문."""
    _agent(client, "silent-agent")
    _todo(client, title="RQRT pull 우회", labels=["auto"])
    r = client.post("/pull", json={"agent": "silent-agent", "require_label": "auto"})
    assert r.status_code == 409 and "receive_mode_undeclared" in r.json()["detail"]
    # 선언 에이전트는 pull 정상
    _agent(client, "hook-agent", mode="hook")
    _todo(client, title="RQRT pull 정상", labels=["auto"])
    ok = client.post("/pull", json={"agent": "hook-agent", "require_label": "auto"})
    assert ok.status_code == 200 and ok.json()["assignee"] == "hook-agent"


def _make_api(client):
    def api(url, path, method="GET", body=None):
        r = client.request(method, path, json=body)
        if r.status_code >= 400:
            raise RuntimeError(f"{method} {path} -> {r.status_code} {r.text[:200]}")
        return r.json() if r.text else None
    return api


def test_probe는_미선언_에이전트에게_배정하지_않는다(monkeypatch, tmp_path):
    """실제 왕복 — 미선언+선언 혼합: 미선언은 후보 제외, 사이클 중단 없음(R1)."""
    from fastapi.testclient import TestClient
    from app import create_app
    app = TestClient(create_app(str(tmp_path / "mix.db")))
    app.post("/agents", json={"name": "silent-agent", "base_url": "https://agent.example/hook"})
    app.post("/agents", json={"name": "declared-agent", "base_url": "https://agent.example/hook",
                              "receive_mode": "hook"})
    app.post("/issues", json={"title": "혼합 배정", "state": "todo", "labels": ["auto"],
                              "acceptance": "완료 기준: 테스트 통과"})
    monkeypatch.setattr(probe_core, "api", _make_api(app))
    monkeypatch.setenv("TT_AUTO_DISPATCH", "1")
    exec_acts = []
    monkeypatch.setattr(probe_core, "execute", lambda url, a: exec_acts.append(a))
    probe_core.run_once("http://x", dry=False)
    assert len(exec_acts) == 1
    assert exec_acts[0]["agent"] == "declared-agent"  # 미선언 후보 제외 — 선언 에이전트만 배정
    # execute는 스텁 — 실제 전이는 claim/pull API 테스트가 담당
