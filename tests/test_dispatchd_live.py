"""dispatchd 실행기 왕복 — 실 서버(TestClient sqlite)에 snapshot/decide/execute 관통.

dispatch webhook는 실제 HTTP가 아니라 agent 미등록 등 서버 검증 경로만 통과하면 된다(30KP:
dispatchd는 public API만 소비). runner 실배송은 BKXA(실 agent) 범위.
"""
import os

import pytest
from fastapi.testclient import TestClient

import dispatchd
import work_contract
from app import create_app


@pytest.fixture
def live(tmp_path, monkeypatch):
    c = TestClient(create_app(str(tmp_path / "d.db")))
    monkeypatch.setattr(dispatchd, "api", _make_api(c))
    return c


def _make_api(client):
    def api(url, path, method="GET", body=None):
        r = client.request(method, path, json=body)
        if r.status_code >= 400:
            raise RuntimeError(f"{method} {path} -> {r.status_code} {r.text[:200]}")
        return r.json() if r.text else None
    return api


def new_issue(c, title, labels=("auto",), parent=None):
    body = {"title": title, "labels": list(labels)}
    if parent:
        body["parent_id"] = parent
    r = c.post("/issues", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def test_round_claims_and_records_dispatch(live):
    os.environ["TT_AUTO_DISPATCH"] = "1"
    try:
        i = new_issue(live, "do work")
        live.post("/agents", json={"name": "r1@mini", "base_url": "http://127.0.0.1:1"})
        snap = dispatchd.snapshot("http://x")
        assert any(x["id"] == i["id"] for x in snap["issues"])
        acts = dispatchd.decide(snap)
        assert acts and acts[0]["issue"] == i["id"] and acts[0]["action"] == "work"
        dispatchd.execute("http://x", acts[0])
        got = live.get(f"/issues/{i['id']}").json()
        assert got["state"] == "in_progress" and got["assignee"] == "r1@mini"
        assert got["lease_by"] == "r1@mini"
        ds = live.get(f"/issues/{i['id']}/dispatches").json()
        assert len(ds) == 1 and "[auto dispatchd]" in ds[0]["message"]
    finally:
        os.environ.pop("TT_AUTO_DISPATCH", None)


def test_needs_human_drops_auto_and_notes(live):
    os.environ["TT_AUTO_DISPATCH"] = "1"
    try:
        i = new_issue(live, "flaky")
        live.post("/agents", json={"name": "r0@mini", "base_url": "http://127.0.0.1:1"})
        for n in range(2):
            live.post(f"/issues/{i['id']}/dispatch",
                      json={"agent": "r0@mini", "message": f"try {n}"})
        snap = dispatchd.snapshot("http://x")
        card = next(x for x in snap["issues"] if x["id"] == i["id"])
        assert card["dispatches"] >= 2
        acts = dispatchd.decide(snap)
        assert acts[0]["action"] == "needs-human"
        dispatchd.execute("http://x", acts[0])
        got = live.get(f"/issues/{i['id']}").json()
        assert "auto" not in got["labels"] and "needs-human" in got["labels"]
        got2 = live.get(f"/issues/{i['id']}").json()
        assert any("[auto]" in c["body"] for c in got2["comments"])
    finally:
        os.environ.pop("TT_AUTO_DISPATCH", None)


@pytest.fixture
def live_v2(tmp_path, monkeypatch):
    monkeypatch.setenv("TT_CONTRACT_VERSION", "2")
    c = TestClient(create_app(str(tmp_path / "v2.db")))
    monkeypatch.setattr(dispatchd, "api", _make_api(c))
    return c


def _claim_contract_issue(c, agent="codex"):
    iid = new_issue(c, "t", labels=("auto",))["id"]
    r = c.post(f"/issues/{iid}/claim", json={"agent": agent, "require_label": "auto"})
    assert r.status_code == 200, r.text
    return r.json()


def test_execute_merge_reported_card_done(live_v2, monkeypatch):
    i = _claim_contract_issue(live_v2)
    iid = i["id"]
    assert i["work_contract"]["version"].startswith("tt-tdd-v2:")
    calls = []
    monkeypatch.setattr(dispatchd, "gh_exec", lambda *a: calls.append(list(map(str, a))) or "")
    import json as _j; print("CARD-REP:", str(live_v2.get(f"/issues/{iid}").json()["completion_report"])[:80], "WC:", live_v2.get(f"/issues/{iid}").json()["work_contract"]["version"][:14])
    rep = {"contract_version": i["work_contract"]["version"], "attempt": i["execution_attempt"],
           "method": "planned",
           "design": {"criteria": "CI green PR 병합", "verification": "live_v2 왕복",
                      "evidence": "수정 전 decide에 merge 없음"},
           "implementation": {"summary": "probe merge 라운드", "commands": "pytest"},
           "verification": {"commands": "pytest -k merge", "evidence": "1 passed"},
           "result": "passed", "limitations": "없음"}
    r = live_v2.patch(f"/issues/{iid}", json={"version": i["version"], "state": "done",
                                              "completion_report": rep})
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "done"
    a = live_v2.get(f"/issues/{iid}").json()
    print("AFTER-DONE-PATCH:", a["state"], a["version"], a["verification_status"])
    dispatchd.execute("/t", {"action": "merge", "issue": iid, "pr": 3})
    b = live_v2.get(f"/issues/{iid}").json()
    print("AFTER-EXEC:", b["state"], b["version"])
    assert calls and calls[0][:2] == ["pr", "merge"]
    got = live_v2.get(f"/issues/{iid}").json()
    assert got["state"] == "done" and got["verification_status"] == "reported"


def test_execute_merge_reportless_demotes_review(live_v2, monkeypatch):
    i = _claim_contract_issue(live_v2)
    monkeypatch.setattr(dispatchd, "gh_exec", lambda *a: "")
    dispatchd.execute("/t", {"action": "merge", "issue": i["id"], "pr": 4})
    assert live_v2.get(f"/issues/{i['id']}").json()["state"] == "review"
