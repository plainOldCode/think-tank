import pytest
from fastapi.testclient import TestClient

from app import create_app


@pytest.fixture
def client(tmp_path):
    app = create_app(str(tmp_path / "tt.db"))
    return TestClient(app)


def mk(client, **kw):
    return client.post("/issues", json={"title": "t", **kw}).json()


def test_create_get(client):
    i = mk(client, body="b", priority=2, labels=["hw"])
    assert i["state"] == "todo"
    got = client.get(f"/issues/{i['id']}").json()
    assert got["labels"] == ["hw"] and got["children"] == [] and got["comments"] == []


def test_parent_child(client):
    p = mk(client, title="parent")
    c1 = mk(client, title="child1", parent_id=p["id"])
    tree = client.get(f"/issues/{p['id']}/tree").json()
    assert tree["tree"][0]["id"] == c1["id"]
    bad = client.post("/issues", json={"title": "x", "parent_id": "NOPE"})
    assert bad.status_code == 404


def test_cycle_blocked(client):
    p = mk(client)
    c = mk(client, parent_id=p["id"])
    r = client.patch(f"/issues/{p['id']}", json={"parent_id": c["id"]})
    assert r.status_code == 422


def test_claim_and_pull(client):
    i = mk(client)
    claimed = client.post(f"/issues/{i['id']}/claim", json={"agent": "a1"})
    assert claimed.json()["state"] == "in_progress" and claimed.json()["assignee"] == "a1"
    again = client.post(f"/issues/{i['id']}/claim", json={"agent": "a2"})
    assert again.status_code == 409
    p1 = mk(client, title="pullable", priority=1)
    pulled = client.post("/pull", json={"agent": "b"}).json()
    assert pulled["id"] == p1["id"] and pulled["assignee"] == "b"
    assert client.post("/pull", json={"agent": "c"}).json() is None


def test_release_resets_assignee(client):
    i = mk(client)
    client.post(f"/issues/{i['id']}/claim", json={"agent": "a1"})
    r = client.patch(f"/issues/{i['id']}", json={"state": "todo"})
    assert r.status_code == 200 and r.json()["assignee"] == ""


def test_transitions(client):
    i = mk(client, state="backlog")
    assert client.patch(f"/issues/{i['id']}", json={"state": "done"}).status_code == 409
    client.patch(f"/issues/{i['id']}", json={"state": "todo"})
    assert client.patch(f"/issues/{i['id']}", json={"state": "done"}).status_code == 409
    client.patch(f"/issues/{i['id']}", json={"state": "in_progress", "assignee": "x"})
    # done≠verified 게이트(M3BZV172-9F0S): 증거 없는 done은 review로 강등(200, completed_at 없음)
    demoted = client.patch(f"/issues/{i['id']}", json={"state": "done"})
    assert demoted.status_code == 200 and demoted.json()["state"] == "review"
    assert demoted.json()["completed_at"] is None
    # review에서 현재 회차의 성공 코멘트로 done; 직접 in_progress 재진입은 불가
    client.post(f"/issues/{i['id']}/comments", json={"author": "x", "body": "작업 완료, commit 0a1b2c3d4e5f 통과"})
    assert client.patch(f"/issues/{i['id']}", json={"state": "in_progress"}).status_code == 409
    done = client.patch(f"/issues/{i['id']}", json={"state": "done"})
    assert done.status_code == 200 and done.json()["completed_at"]
    assert done.json()["verified"] == 1 and "0a1b2c3d4e5f" in done.json()["verified_evidence"]
    reopen = client.patch(f"/issues/{i['id']}", json={"state": "todo"})
    assert reopen.status_code == 200 and reopen.json()["assignee"] == ""


def test_version_conflict(client):
    i = mk(client)
    stale = i["version"]
    client.patch(f"/issues/{i['id']}", json={"title": "v2"})
    r = client.patch(f"/issues/{i['id']}", json={"title": "v3"}, headers={}) if False else \
        client.request("PATCH", f"/issues/{i['id']}", json={"title": "v3", "expected_version": stale})
    assert r.status_code == 409


def test_comments(client):
    i = mk(client)
    client.post(f"/issues/{i['id']}/comments", json={"author": "opencode", "body": "did x"})
    got = client.get(f"/issues/{i['id']}").json()
    assert got["comments"][0]["body"] == "did x"


def test_filters(client):
    a = mk(client, title="alpha", labels=["fw"], state="backlog")
    mk(client, title="beta")
    assert [x["id"] for x in client.get("/issues?state=backlog").json()] == [a["id"]]
    assert [x["id"] for x in client.get("/issues?label=fw").json()] == [a["id"]]
    assert mk(client, parent_id=a["id"])
    roots = client.get("/issues?parent=none&q=alph").json()
    assert len(roots) == 1


def test_archive_hides_from_board_and_pull(client):
    i = mk(client, title="old")
    assert i["archived"] == 0
    r = client.patch(f"/issues/{i['id']}", json={"archived": True})
    assert r.status_code == 200 and r.json()["archived"] == 1
    ids = [x["id"] for x in client.get("/issues").json()]
    assert i["id"] not in ids
    ids_all = [x["id"] for x in client.get("/issues?archived=all").json()]
    assert i["id"] in ids_all
    ids_only = [x["id"] for x in client.get("/issues?archived=only").json()]
    assert ids_only == [i["id"]]
    assert client.post("/pull", json={"agent": "a"}).json() is None
    back = client.patch(f"/issues/{i['id']}", json={"archived": False})
    assert back.json()["archived"] == 0
    assert client.post("/pull", json={"agent": "a"}).json()["id"] == i["id"]


def test_todo_to_backlog_demote(client):
    i = mk(client)
    r = client.patch(f"/issues/{i['id']}", json={"state": "backlog"})
    assert r.status_code == 200 and r.json()["state"] == "backlog" and r.json()["assignee"] == ""
    assert client.patch(f"/issues/{i['id']}", json={"state": "todo"}).status_code == 200


def test_lease_heartbeat_and_release(client):
    i = mk(client)
    a = client.post(f"/issues/{i['id']}/claim", json={"agent": "bot"}).json()
    assert a["lease_by"] == "bot" and a["lease_expires"]
    assert client.post(f"/issues/{i['id']}/lease", json={"agent": "other"}).status_code == 409
    hb = client.post(f"/issues/{i['id']}/lease", json={"agent": "bot"})
    assert hb.status_code == 200 and hb.json()["heartbeat_at"]
    assert hb.json()["version"] == a["version"]  # heartbeat는 버전 안 올림(충돌 유발 금지)
    client.patch(f"/issues/{i['id']}", json={"state": "todo"})
    gone = client.get(f"/issues/{i['id']}").json()
    assert gone["lease_by"] == "" and gone["lease_expires"] is None


def test_pull_steals_expired_lease(client, tmp_path):
    import sqlite3
    i = mk(client, title="zombie")
    client.post(f"/issues/{i['id']}/claim", json={"agent": "crashed@x", "hours": 1})
    db = sqlite3.connect(str(tmp_path / "tt.db"))
    db.execute("UPDATE issues SET lease_expires='2020-01-01T00:00:00+0900' WHERE id=?", (i["id"],))
    db.commit()
    assert client.get(f"/issues/{i['id']}").json()["state"] == "in_progress"
    got = client.post("/pull", json={"agent": "rescuer"}).json()
    assert got["id"] == i["id"] and got["assignee"] == "rescuer"
    assert got["lease_by"] == "rescuer"


def test_pull_label_gate(client):
    mk(client, title="plain")
    auto = mk(client, title="auto", labels=["auto"])
    assert client.post("/pull", json={"agent": "cron", "require_label": "auto"}).json()["id"] == auto["id"]


def test_pull_lease_limit(client):
    a = mk(client, title="a1")
    b = mk(client, title="a2")
    c = mk(client, title="a3")
    for x in (a, b):
        assert client.post("/pull", json={"agent": "hog"}).json()["id"] == x["id"]
    assert client.post("/pull", json={"agent": "hog"}).status_code == 409
    assert client.post("/pull", json={"agent": "fresh"}).json()["id"] == c["id"]


def test_lease_hours_clamped(client):
    import datetime
    import db as dbmod
    i = mk(client)
    a = client.post(f"/issues/{i['id']}/claim", json={"agent": "bot", "hours": 24}).json()
    exp = datetime.datetime.strptime(a["lease_expires"], "%Y-%m-%dT%H:%M:%S%z")
    now = datetime.datetime.strptime(dbmod.now(), "%Y-%m-%dT%H:%M:%S%z")
    assert 5 * 3600 < (exp - now).total_seconds() <= 6 * 3600 + 2


def test_install_script(client):
    r = client.get("/install.sh")
    assert r.status_code == 200
    body = r.text
    assert body.startswith("#!/bin/sh")
    assert "http://testserver" in body and "127.0.0.1:7800" not in body
    assert "chmod +x" in body and "TT_EOF" in body


def test_claim_respects_lease_limit(client):
    agent = "lim@test"
    ids = [client.post("/issues", json={"title": f"L{i}", "state": "todo"}).json()["id"] for i in range(3)]
    assert client.post(f"/issues/{ids[0]}/claim", json={"agent": agent}).status_code == 200
    assert client.post(f"/issues/{ids[1]}/claim", json={"agent": agent}).status_code == 200
    assert client.post(f"/issues/{ids[2]}/claim", json={"agent": agent}).status_code == 409


def test_unassign_clears_lease(client):
    iid = client.post("/issues", json={"title": "U", "state": "todo"}).json()["id"]
    client.post(f"/issues/{iid}/claim", json={"agent": "ua@test"})
    r = client.patch(f"/issues/{iid}", json={"assignee": ""})
    assert r.status_code == 200 and r.json()["lease_by"] == "" and r.json()["assignee"] == ""


def test_parent_done_requires_children_closed(client):
    p = client.post("/issues", json={"title": "P"}).json()["id"]
    c1 = client.post("/issues", json={"title": "C1", "state": "todo"}).json()["id"]
    c2 = client.post("/issues", json={"title": "C2", "state": "todo"}).json()["id"]
    client.patch(f"/issues/{c1}", json={"parent_id": p})
    client.patch(f"/issues/{c2}", json={"parent_id": p})
    client.patch(f"/issues/{p}", json={"state": "todo"})
    client.post(f"/issues/{p}/claim", json={"agent": "g@test"})
    client.post(f"/issues/{c1}/claim", json={"agent": "g1@test"})
    assert client.patch(f"/issues/{p}", json={"state": "done"}).status_code == 409
    client.patch(f"/issues/{c1}", json={"state": "done", "force_done": True})
    assert client.patch(f"/issues/{p}", json={"state": "done"}).status_code == 409
    client.post(f"/issues/{c2}/claim", json={"agent": "g2@test"})
    client.patch(f"/issues/{c2}", json={"state": "done", "force_done": True})
    assert client.patch(f"/issues/{p}", json={"state": "done", "force_done": True}).status_code == 200


def test_claim_self_assigned(client):
    iid = client.post("/issues", json={"title": "A", "state": "todo"}).json()["id"]
    client.patch(f"/issues/{iid}", json={"assignee": "me@test"})
    assert client.post(f"/issues/{iid}/claim", json={"agent": "me@test"}).status_code == 200
    assert client.post(f"/issues/{iid}/claim", json={"agent": "other@test"}).status_code == 409


# ---- agent registry + dispatch (hook/callback) ----

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer


class Hook(BaseHTTPRequestHandler):
    received = []
    mode = "ok"

    def do_POST(self):
        n = int(self.headers.get("content-length", 0))
        Hook.received.append(({k.lower(): v for k, v in self.headers.items()}, json.loads(self.rfile.read(n))))
        if Hook.mode == "fail":
            self.send_response(500)
            self.end_headers()
            return
        payload = json.dumps({"context": "ses_X1"}).encode() if Hook.mode == "ctx" else b"{}"
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *a):
        pass


@pytest.fixture
def hook_server():
    Hook.received, Hook.mode = [], "ok"
    srv = HTTPServer(("127.0.0.1", 0), Hook)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/hook"
    srv.shutdown()


def test_agent_crud(client):
    assert client.post("/agents", json={"name": "h", "base_url": "ftp://x"}).status_code == 422
    a = client.post("/agents", json={"name": "hermes", "base_url": "http://x/hook", "secret": "s"}).json()
    assert a["enabled"] == 1 and a["secret"] == "s"
    assert client.post("/agents", json={"name": "hermes", "base_url": "http://x"}).status_code == 409
    assert client.patch("/agents/hermes", json={"enabled": False}).json()["enabled"] == 0
    assert client.patch("/agents/hermes", json={"enabled": True}).json()["enabled"] == 1
    assert len(client.get("/agents").json()) == 1
    assert client.delete("/agents/nope").status_code == 404
    assert client.delete("/agents/hermes").json() == {"ok": True}


def test_dispatch_roundtrip(client, hook_server):
    i = mk(client, title="대화 카드")
    client.post("/agents", json={"name": "demo", "base_url": hook_server, "secret": "sekret"})
    d = client.post(f"/issues/{i['id']}/dispatch",
                    json={"agent": "demo", "message": "이거 해줘", "author": "human"}).json()
    assert d["status"] == "ok" and d["agent"] == "demo"
    headers, body = Hook.received[0]
    assert headers.get("authorization") == "Bearer sekret"
    assert body["issue_id"] == i["id"] and body["message"] == "이거 해줘"
    assert body["author"] == "human" and body["issue_title"] == "대화 카드"
    assert body["tt_url"].startswith("http") and body["context"] == ""
    got = client.get(f"/issues/{i['id']}").json()
    assert any(cm["body"] == "이거 해줘" for cm in got["comments"])
    # agent 회신 댓글 → 상세(보드 폴링)에 실시간 노출
    client.post(f"/issues/{i['id']}/comments", json={"author": "demo", "body": "추가 질문: 대상 파일?"})
    assert any("추가 질문" in cm["body"] for cm in client.get(f"/issues/{i['id']}").json()["comments"])


def test_dispatch_context_resume(client, hook_server):
    Hook.mode = "ctx"
    i = mk(client, title="resume")
    client.post("/agents", json={"name": "demo", "base_url": hook_server})
    d1 = client.post(f"/issues/{i['id']}/dispatch", json={"agent": "demo", "message": "1차"}).json()
    assert d1["context"] == "ses_X1"
    client.post(f"/issues/{i['id']}/dispatch", json={"agent": "demo", "message": "2차"})
    assert Hook.received[1][1]["context"] == "ses_X1"
    assert Hook.received[1][1]["comments"][-2]["body"] == "1차"


def test_dispatch_failure_system_comment(client, hook_server):
    Hook.mode = "fail"
    i = mk(client, title="실패 경로")
    client.post("/agents", json={"name": "demo", "base_url": hook_server})
    d = client.post(f"/issues/{i['id']}/dispatch", json={"agent": "demo", "message": "메시지"}).json()
    assert d["status"] == "error" and "500" in d["detail"]
    comments = client.get(f"/issues/{i['id']}").json()["comments"]
    assert any(c["author"] == "tt-server" and "실패" in c["body"] for c in comments)
    assert client.get("/agents").json()[0]["last_err"] != ""


def test_dispatch_guards(client, hook_server):
    i = mk(client, title="가드")
    assert client.post(f"/issues/{i['id']}/dispatch", json={"agent": "ghost", "message": "x"}).status_code == 404
    client.post("/agents", json={"name": "off", "base_url": hook_server, "enabled": False})
    assert client.post(f"/issues/{i['id']}/dispatch", json={"agent": "off", "message": "x"}).status_code == 409
    client.post("/agents", json={"name": "on", "base_url": hook_server})
    assert client.post(f"/issues/{i['id']}/dispatch", json={"agent": "on", "message": " "}).status_code == 422


# ---- blocked 지능화 (M3BZS1FS-5722): waiting_for/why-blocked/reconcile/④ ----

def to_blocked(client, iid, **kw):
    client.patch(f"/issues/{iid}", json={"state": "in_progress", "assignee": "w@t"})
    r = client.patch(f"/issues/{iid}", json={"state": "blocked", **kw})
    assert r.status_code == 200, r.text
    return r.json()


def test_waiting_for_fields_and_backcompat(client):
    i = mk(client)
    # 미입력 blocked: 기존 동작 그대로 (필드 빈 값)
    b = to_blocked(client, i["id"])
    assert b["waiting_for"] == "" and b["waiting_actor"] == "" and b["blocked_detail"] == ""
    # enum 검증
    assert client.patch(f"/issues/{i['id']}",
                        json={"waiting_for": "vibes"}).status_code == 422
    # blocked 상태에서는 사족 보강 가능
    r = client.patch(f"/issues/{i['id']}",
                     json={"waiting_for": "human", "waiting_actor": "user@mini", "blocked_detail": "스펙 확인"})
    assert r.status_code == 200
    assert r.json()["waiting_for"] == "human" and r.json()["waiting_actor"] == "user@mini"
    # blocked가 아닌 상태에서는 waiting_for 설정 거부
    j = mk(client)
    assert client.patch(f"/issues/{j['id']}", json={"waiting_for": "human"}).status_code == 409
    # blocked 이탈 시 사족 자동 소거
    back = client.patch(f"/issues/{i['id']}", json={"state": "todo"})
    assert back.json()["waiting_for"] == "" and back.json()["waiting_actor"] == ""


def test_waiting_for_on_transition(client):
    i = mk(client)
    b = to_blocked(client, i["id"], waiting_for="gate", blocked_detail="rm 패턴 게이트")
    assert b["waiting_for"] == "gate" and b["blocked_detail"] == "rm 패턴 게이트"


def test_why_blocked_projection(client):
    dep = mk(client, title="선행 작업")
    i = mk(client, title="막힌 카드")
    to_blocked(client, i["id"], waiting_for="dependency",
               blocked_detail=f"의존: {dep['id']} 완료 후 재개")
    client.post(f"/issues/{i['id']}/comments",
                json={"author": "runner:m2max", "body": "dispatch#7 BLOCKED — waiting_for=human"})
    r = client.get(f"/issues/{i['id']}/why-blocked")
    assert r.status_code == 200
    w = r.json()
    assert w["gate"]["kind"] == "dependency"
    assert w["waiting_for_source"] == "field"
    assert w["dependencies"] == [{"id": dep["id"], "state": "todo"}]
    assert w["missing"] == [dep["id"]] and w["release_ready"] is False
    assert any(dep["id"] in c for c in w["next_commands"])
    assert "BLOCKED" in w["evidence"]
    # 비-blocked는 409, legacy 코멘트 추정, 의존 done → release_ready
    assert client.get(f"/issues/{dep['id']}/why-blocked").status_code == 409
    j = mk(client, title="레거시")
    to_blocked(client, j["id"])
    client.post(f"/issues/{j['id']}/comments",
                json={"author": "runner:x", "body": "BLOCKED — waiting_for=human 발생"})
    w2 = client.get(f"/issues/{j['id']}/why-blocked").json()
    assert w2["waiting_for"] == "human" and w2["waiting_for_source"] == "comment"
    client.patch(f"/issues/{dep['id']}", json={"state": "in_progress", "assignee": "z@t"})
    client.patch(f"/issues/{dep['id']}", json={"state": "done", "force_done": True})
    w3 = client.get(f"/issues/{i['id']}/why-blocked").json()
    assert w3["release_ready"] is True and w3["missing"] == []


def test_list_and_detail_expose_release_ready(client):
    dep = mk(client, title="dep")
    i = mk(client, title="waiting")
    to_blocked(client, i["id"], waiting_for="dependency", blocked_detail=dep["id"])
    assert client.get(f"/issues/{i['id']}").json()["release_ready"] is False
    assert any(x["id"] == i["id"] and x["release_ready"] is False
               for x in client.get("/issues?state=blocked").json())
    client.patch(f"/issues/{dep['id']}", json={"state": "in_progress", "assignee": "z@t"})
    client.patch(f"/issues/{dep['id']}", json={"state": "done", "force_done": True})
    got = client.get(f"/issues/{i['id']}").json()
    assert got["release_ready"] is True
    # ④ 의존 종료 시 '해제 가능' 시스템 댓글, 중복 없음, 자동 상태 변경 없음
    notes = [c["body"] for c in got["comments"] if c["author"] == "tt-server" and "release-ready" in c["body"]]
    assert len(notes) == 1 and "해제 가능" in notes[0] and "자동 재dispatch 없음" in notes[0]
    assert got["state"] == "blocked"


def test_reconcile_release_command(client, hook_server):
    # dispatch 성공 이력 + release_hook 에이전트 → terminalize 시 release 명령 발송
    Hook.received, Hook.mode = [], "ok"
    i = mk(client, title="실행 중 취소")
    client.post("/agents", json={"name": "runner-x", "base_url": hook_server,
                                 "secret": "sekret", "release_hook": True})
    client.post(f"/issues/{i['id']}/dispatch", json={"agent": "runner-x", "message": "실행해"})
    did = Hook.received[0][1]["dispatch_id"]
    client.post(f"/issues/{i['id']}/claim", json={"agent": "runner-x"})
    Hook.received.clear()
    r = client.patch(f"/issues/{i['id']}", json={"state": "done", "force_done": True})
    assert r.status_code == 200, r.text
    headers, body = Hook.received[-1]
    assert headers.get("x-tt-command") == "release"
    assert headers.get("authorization") == "Bearer sekret"
    assert body["command"] == "release" and body["issue_id"] == i["id"]
    assert body["dispatch_id"] == did
    # dispatch가 아니라 release로 들어왔으므로 댓글은 dispatch message가 하나만 있어야 함
    comments = client.get(f"/issues/{i['id']}").json()["comments"]
    assert any(c["author"] == "tt-server" and "reconcile release" in c["body"] for c in comments)


def test_reconcile_skips_non_release_hook_agents(client, hook_server):
    i = mk(client, title="release_hook off")
    client.post("/agents", json={"name": "plain", "base_url": hook_server})
    client.post(f"/issues/{i['id']}/dispatch", json={"agent": "plain", "message": "실행"})
    Hook.received.clear()
    client.patch(f"/issues/{i['id']}", json={"state": "cancelled"})
    assert all("x-tt-command" not in h for h, _ in Hook.received)
    # dispatch 이력 없는 카드도 release 발송 없음·전이 성공
    j = mk(client, title="no dispatch")
    assert client.patch(f"/issues/{j['id']}", json={"state": "in_progress", "assignee": "a@t"}).status_code == 200
    assert client.patch(f"/issues/{j['id']}", json={"state": "done"}).status_code == 200


def test_agent_release_hook_flag(client):
    a = client.post("/agents", json={"name": "r1", "base_url": "http://x/h", "release_hook": True}).json()
    assert a["release_hook"] == 1
    b = client.post("/agents", json={"name": "r2", "base_url": "http://x/h"}).json()
    assert b["release_hook"] == 0
    assert client.patch("/agents/r2", json={"release_hook": True}).json()["release_hook"] == 1


# ---- done≠verified 게이트 + blocked→human Level4 알림 (M3BZV172-9F0S) ----

def test_done_gate_demotes_without_evidence(client):
    i = mk(client, title="증거 없는 완료 시도")
    client.post(f"/issues/{i['id']}/claim", json={"agent": "w@t"})
    client.post(f"/issues/{i['id']}/comments", json={"author": "w@t", "body": "다 했습니다 화이팅"})
    r = client.patch(f"/issues/{i['id']}", json={"state": "done"})
    assert r.status_code == 200 and r.json()["state"] == "review"
    assert r.json()["lease_by"] == ""  # review 강등 시 worker lease 반납
    got = client.get(f"/issues/{i['id']}").json()
    gate_note = [c["body"] for c in got["comments"] if c["author"] == "tt-server" and "done 증거 없음" in c["body"]]
    assert len(gate_note) == 1 and "tt verify" in gate_note[0]
    # review 상태의 카드 pull 금지 확인(state가 todo만 대상)
    assert client.post("/pull", json={"agent": "other"}).json() is None


def test_close_label_bypasses_done_gate(client):
    # bridge 규약: close 라벨(사람 승인 마감)은 기존 done 경로 유지 — 충돌 금지
    i = mk(client, title="close 라벨 마감", labels=["close"])
    client.post(f"/issues/{i['id']}/claim", json={"agent": "w@t"})
    r = client.patch(f"/issues/{i['id']}", json={"state": "done"})
    assert r.status_code == 200 and r.json()["state"] == "done"
    assert r.json()["verified"] == 1 and "승인 경로" in r.json()["verified_evidence"]


def test_verify_endpoint_review_to_done(client):
    dep = mk(client, title="verify 의존 child")
    p = mk(client, title="verify 부모")
    client.patch(f"/issues/{dep['id']}", json={"parent_id": p["id"]})
    client.post(f"/issues/{p['id']}/claim", json={"agent": "w@t"})
    client.post(f"/issues/{dep['id']}/claim", json={"agent": "c@t"})
    # 부모 done 가드는 게이트보다 먼저: child 미완료면 409 (기존 동작 유지)
    assert client.patch(f"/issues/{p['id']}", json={"state": "done"}).status_code == 409
    client.patch(f"/issues/{dep['id']}", json={"state": "done", "force_done": True})
    demoted = client.patch(f"/issues/{p['id']}", json={"state": "done"})
    assert demoted.json()["state"] == "review"
    # review인데 child 재오픈 → verify도 부모 가드 409
    client.patch(f"/issues/{dep['id']}", json={"state": "todo"})
    client.post(f"/issues/{dep['id']}/claim", json={"agent": "c@t"})
    assert client.post(f"/issues/{p['id']}/verify",
                       json={"verifier": "user", "evidence": "abc1234"}).status_code == 409
    client.patch(f"/issues/{dep['id']}", json={"state": "done", "force_done": True})
    # done은 verified evidence 필수 — 코멘트에 (agent) 증거 없으면 422 (시스템 댓글은 근거 불인정)
    assert client.post(f"/issues/{p['id']}/verify", json={"verifier": "user"}).status_code == 422
    v = client.post(f"/issues/{p['id']}/verify", json={"verifier": "user@mini", "evidence": "pytest 33 passed"})
    assert v.status_code == 200 and v.json()["state"] == "done" and v.json()["verified"] == 1
    assert "pytest 33 passed" in v.json()["verified_evidence"]
    # 비-review는 verify 불가
    assert client.post(f"/issues/{p['id']}/verify",
                       json={"verifier": "u", "evidence": "x"}).status_code == 409


def test_warn_mode_allows_done_with_comment(client, tmp_path):
    import importlib
    import os
    import app as appmod
    os.environ["TT_DONE_GATE"] = "warn"
    m = importlib.reload(appmod)
    try:
        c = m.create_app(str(tmp_path / "warn.db"))
        from fastapi.testclient import TestClient
        wc = TestClient(c)
        i = wc.post("/issues", json={"title": "warn"}).json()
        wc.post(f"/issues/{i['id']}/claim", json={"agent": "w@t"})
        r = wc.patch(f"/issues/{i['id']}", json={"state": "done"})
        assert r.status_code == 200 and r.json()["state"] == "done"
        assert r.json()["verified"] == 0
        got = wc.get(f"/issues/{i['id']}").json()
        assert r.json()["completed_at"] and got["verified"] == 0
        assert any("tt-server" == cm["author"] and "완료증거" in cm["body"]
                   for cm in got["comments"])
    finally:
        os.environ.pop("TT_DONE_GATE", None)
        importlib.reload(appmod)


def test_blocked_human_notify_level4(client, hook_server):
    # notify_hook agent 등록 → blocked(waiting_for=human) 시 Level4 알림 1회 + dedup
    Hook.received, Hook.mode = [], "ok"
    client.post("/agents", json={"name": "notify-x", "base_url": hook_server,
                                 "secret": "sekret", "notify_hook": True})
    i = mk(client, title="사람 대기 카드")
    b = to_blocked(client, i["id"], waiting_for="human", waiting_actor="user@mini",
                   blocked_detail="A/B 선택 필요")
    assert b["blocked_notified_at"]
    hdrs, body = Hook.received[-1]
    assert hdrs.get("x-tt-command") == "notify"
    assert hdrs.get("authorization") == "Bearer sekret"
    assert body["command"] == "notify" and body["issue_id"] == i["id"]
    text = body["text"]
    assert "Level4" in text and "[A]" in text and "[B]" in text  # A/B 선택지
    assert "권장: B" in text  # human → recommendation B(결정 후 재개)
    notes = [c["body"] for c in client.get(f"/issues/{i['id']}").json()["comments"]
             if c["author"] == "tt-server" and "[level4-notify]" in c["body"]]
    assert len(notes) == 1
    # 같은 카드 재-blocked(notified 필드 유지) → 추가 발송 없음 (dedup)
    client.patch(f"/issues/{i['id']}", json={"state": "todo"})
    client.patch(f"/issues/{i['id']}", json={"state": "in_progress", "assignee": "w@t"})
    client.patch(f"/issues/{i['id']}", json={"state": "blocked", "waiting_for": "human"})
    # 이탈 시 notified 리셋 → 재알림은 '새 대기'이므로 1회 더 허용 (총 2), 중복 자체는 없음
    notes2 = [c["body"] for c in client.get(f"/issues/{i['id']}").json()["comments"]
              if c["author"] == "tt-server" and "[level4-notify]" in c["body"]]
    assert len(notes2) == 2
    n_notify = sum(1 for h, _ in Hook.received if h.get("x-tt-command") == "notify")
    assert n_notify == 2


def test_blocked_comment_marker_notify_legacy(client, hook_server):
    # legacy runner BLOCKED 코멘트(waiting_for=human 문자열)로도 알림 발동 — runner 무수정 호환
    Hook.received, Hook.mode = [], "ok"
    client.post("/agents", json={"name": "notify-x", "base_url": hook_server, "notify_hook": True})
    i = mk(client, title="레거시 러너 보류")
    to_blocked(client, i["id"])  # 사족 없는 blocked — 이 순간 알림 없음
    assert sum(1 for h, _ in Hook.received if h.get("x-tt-command") == "notify") == 0
    client.post(f"/issues/{i['id']}/comments",
                json={"author": "runner:m2max", "body": "dispatch#9 BLOCKED — waiting_for=human 입력 대기"})
    assert sum(1 for h, _ in Hook.received if h.get("x-tt-command") == "notify") == 1
    # 같은 마커 중복 코멘트 → dedup
    client.post(f"/issues/{i['id']}/comments",
                json={"author": "runner:m2max", "body": "dispatch#9 BLOCKED — waiting_for=human 재확인"})
    assert sum(1 for h, _ in Hook.received if h.get("x-tt-command") == "notify") == 1


def test_notify_skips_agents_without_flag(client, hook_server):
    Hook.received, Hook.mode = [], "ok"
    client.post("/agents", json={"name": "plain", "base_url": hook_server})  # notify_hook off
    i = mk(client, title="알림 없음")
    to_blocked(client, i["id"], waiting_for="human")
    assert all(h.get("x-tt-command") != "notify" for h, _ in Hook.received)


def test_notify_relative_url_needs_base(client):
    # TT_NOTIFY_BASE 미설정 시 상대경로 base_url 거부 (설정 시에만 해석 — 오발사 방지)
    assert client.post("/agents", json={"name": "rel", "base_url": "/webhooks/tt-notify"}).status_code == 422


def test_notify_hook_flag_toggle(client):
    a = client.post("/agents", json={"name": "n1", "base_url": "http://x/h", "notify_hook": True}).json()
    assert a["notify_hook"] == 1
    assert client.patch("/agents/n1", json={"notify_hook": False}).json()["notify_hook"] == 0
