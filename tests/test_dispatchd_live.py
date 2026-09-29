"""dispatchd 실행기 왕복 — 실 서버(TestClient sqlite)에 snapshot/decide/execute 관통.

dispatch webhook는 실제 HTTP가 아니라 agent 미등록 등 서버 검증 경로만 통과하면 된다(30KP:
dispatchd는 public API만 소비). runner 실배송은 BKXA(실 agent) 범위.
"""
import os

import pytest
from fastapi.testclient import TestClient

import dispatchd
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
