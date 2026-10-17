"""M3B5JHGP(M54EAGJW-48HZ) — pending inbox 폴링 채널: 포트 없는 에이전트 양방향 수신.

codex·claude류 CLI 에이전트는 수신 포트가 없다(opencode TUI·hermes 브리지와 다름).
receive_mode=poll + runner_name 선언 시 훅 전달 없이 큐에 쌓이고, 소속 러너가
GET /agents/{runner}/pending으로 꺼내 간다 — webhook 단일 채널의 이중 전달 없음.
"""
import json

import pytest


@pytest.fixture
def runner_agent(client):
    """poll 수신 에이전트 + 소속 러너 시크릿."""
    from server import db as dbmod
    with client.app.state.ctx.con() as c:
        c.execute("INSERT INTO agents (name, base_url, secret, enabled, receive_mode, runner_name, aliases, created_at) "
                  "VALUES ('claude-tp', '', 'pollsecret', 1, 'poll', 'runner-tp13', '', ?)",
                  (dbmod.now(),))
        c.commit()
    return "claude-tp"


def _mk_issue(client, title="[t] inbox", body="b", acceptance="a"):
    r = client.post("/issues", json={"title": title, "body": body, "acceptance": acceptance})
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def test_poll_agent_requires_runner_name(client):
    r = client.post("/agents", json={"name": "bad-poll", "receive_mode": "poll"})
    assert r.status_code == 422
    assert "runner_name" in r.json()["detail"]


def test_poll_agent_registers_without_base_url(client):
    r = client.post("/agents", json={"name": "poll-x", "receive_mode": "poll",
                                     "runner_name": "runner-x", "secret": "s1"})
    assert r.status_code in (200, 201), r.text
    d = r.json()
    assert d["receive_mode"] == "poll" and d["base_url"] is None
    assert "secret" not in d  # K4Q7 직렬화 관문 유지


def test_dispatch_to_poll_agent_skips_hook_and_stays_queued(client, runner_agent):
    iid = _mk_issue(client)
    r = client.post(f"/issues/{iid}/dispatch",
                    json={"agent": runner_agent, "message": "poll 로 수령해라", "author": "tester"})
    assert r.status_code in (200, 201), r.text
    d = r.json()
    assert d["status"] == "queued", "poll 에이전트는 훅 POST 없이 큐에 남는다"
    # 지시문은 코멘트로 이미 기록(관찰 가능성)
    bodies = [c["body"] for c in client.get(f"/issues/{iid}").json()["comments"]]
    assert any("poll 로 수령해라" in b for b in bodies)


def test_pending_requires_runner_secret(client, runner_agent):
    r = client.get("/agents/runner-tp13/pending",
                   headers={"authorization": "Bearer wrong"})
    assert r.status_code == 401


def test_pending_hands_out_queued_once(client, runner_agent):
    iid = _mk_issue(client)
    r = client.post(f"/issues/{iid}/dispatch",
                    json={"agent": runner_agent, "message": "m1", "author": "tester"})
    assert r.status_code in (200, 201)
    hdr = {"authorization": "Bearer pollsecret"}
    out = client.get("/agents/runner-tp13/pending", headers=hdr)
    assert out.status_code == 200, out.text
    items = out.json()
    assert len(items) == 1
    p = items[0]
    assert p["dispatch_id"] and p["issue_id"] == iid
    assert p["message"] == "m1" and p["agent"] == runner_agent
    assert p["issue_title"] and p["tt_url"], "훅 payload와 동일한 형태여야 러너 deliver_item이 소비한다"
    # 2회차 — 이중 전달 없음
    assert client.get("/agents/runner-tp13/pending", headers=hdr).json() == []
    row = client.get(f"/issues/{iid}/dispatches").json()[-1]
    assert row["status"] == "ok" and "poll" in (row["detail"] or "")


def test_pending_ignores_other_runners_secret(client, runner_agent):
    from server import db as dbmod
    with client.app.state.ctx.con() as c:
        c.execute("INSERT INTO agents (name, base_url, secret, enabled, receive_mode, runner_name, aliases, created_at) "
                  "VALUES ('claude-tp2', '', '', 1, 'poll', 'runner-other', '', ?)", (dbmod.now(),))
        c.commit()
    r = client.get("/agents/runner-other/pending",
                   headers={"authorization": "Bearer pollsecret"})
    assert r.status_code == 401, "러너 시크릿은 자기 소속 에이전트의 것만 통과"


def test_probe_assignment_includes_poll_agents(client, runner_agent):
    """프로브 배정 후보 필터가 poll 에이전트를 배제하지 않는다(배정은 선언까지 요구)."""
    from server.probe import core
    a = {"name": "claude-tp", "enabled": 1, "base_url": None,
         "receive_mode": "poll", "runner_name": "runner-tp13"}
    assert core._receivable(a) is True and core._assignable(a) is True
    # 구 등록분 호환: mode 미선언 hook은 전달은 되지만 자동 배정은 안 된다(RQRT)
    assert core._receivable({"name": "x", "enabled": 1, "base_url": "http://h/hook"}) is True
    assert core._assignable({"name": "x", "enabled": 1, "base_url": "http://h/hook"}) is False
    assert core._receivable({"name": "y", "enabled": 1, "base_url": "http://h/hook",
                             "receive_mode": "hook"}) is True
    assert core._receivable({"name": "z", "enabled": 1, "receive_mode": "poll"}) is False


def test_runner_poll_sends_bearer_secret(monkeypatch, tmp_path):
    """러너 poll_once가 pending 조회에 Bearer 시크릿을 붙인다."""
    import importlib.util
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "runner"
    monkeypatch.syspath_prepend(str(root))
    monkeypatch.setenv("TT_RUNNER_STATE", str(tmp_path / "runner"))
    monkeypatch.setenv("TT_RUNNER_SECRET", str(tmp_path / "secret"))
    (tmp_path / "secret").write_text("pollsecret")
    monkeypatch.setenv("TT_RUNNER_REGISTRY", str(tmp_path / "registry.json"))
    monkeypatch.setenv("TT_URL", "http://127.0.0.1:1")
    spec = importlib.util.spec_from_file_location("iso_runner_inbox", root / "tt-runner.py")
    r = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(r)
    seen = {}

    def fake_http(method, path, payload=None, timeout=15, auth=False):
        seen["path"], seen["auth"] = path, auth
        return []

    monkeypatch.setattr(r, "tt_http", fake_http)
    r.poll_once()
    assert seen["path"] == "/agents/" + r.RUNNER_NAME + "/pending"
    assert seen["auth"] is True
