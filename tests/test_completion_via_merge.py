"""완료 전이 개편 (M3R7M0ZR-YF99): done은 probe 병합(verify) 또는 사람 승인만.

agent의 유효보고 done 요청은 review로 정지(보고 보존), review→done은 verify 전용.
dispatchd: review 카드의 green PR merge → verify로 done; 병합 불가 review 카드는
needs-merge 코멘트 1회(회차 dedup).

RED 기준: 첫·둘·넷·다섯·여섯·일곱·여덟·열 은 수정 전 실패해야 한다.
(셋 close 라벨 예외·아홉 merge 판정은 기존 동작 유지 = 수정 전에도 통과.)
"""
import importlib

import pytest
from fastapi.testclient import TestClient

import app as appmod
import dispatchd


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("TT_REQUIRE_REPORT", "1")
    monkeypatch.setenv("TT_CONTRACT_VERSION", "2")
    m = importlib.reload(appmod)
    return TestClient(m.create_app(str(tmp_path / "tt.db")))


def v2_report(contract, attempt=1):
    return {
        "contract_version": contract,
        "attempt": attempt,
        "method": "planned",
        "design": {"criteria": "c", "verification": "v"},
        "implementation": {"summary": "s", "commands": "cc"},
        "verification": {"commands": "ccc", "evidence": "e"},
        "result": "passed",
        "limitations": "",
    }


def claimed(client):
    i = client.post("/issues", json={"title": "병합 게이트"}).json()
    c = client.post(f"/issues/{i['id']}/claim", json={"agent": "w@t"}).json()
    return i["id"], c["work_contract"]["version"], c["execution_attempt"]


def test_유효보고_done요청은_review에_정지(client):
    iid, ver, att = claimed(client)
    r = client.patch(f"/issues/{iid}",
                     json={"state": "done", "completion_report": v2_report(ver, att)}).json()
    assert r["state"] == "review"
    assert r["lease_by"] == ""
    assert r["completion_report"]
    got = client.get(f"/issues/{iid}").json()
    assert any("보고 접수" in c["body"] for c in got["comments"] if c["author"] == "tt-server")


def test_verify로만_review에서_done(client):
    iid, ver, att = claimed(client)
    rep = v2_report(ver, att)
    client.patch(f"/issues/{iid}", json={"state": "done", "completion_report": rep})
    bad = client.post(f"/issues/{iid}/verify",
                      json={"verifier": "probe", "completion_report": v2_report(ver, att + 99)})
    assert bad.status_code == 409
    ok = client.post(f"/issues/{iid}/verify",
                     json={"verifier": "probe", "completion_report": rep})
    assert ok.status_code == 200 and ok.json()["state"] == "done"


def test_근거없는_done은_강등코멘트(client):
    iid, ver, att = claimed(client)
    r = client.patch(f"/issues/{iid}", json={"state": "done"}).json()
    assert r["state"] == "review"
    got = client.get(f"/issues/{iid}").json()
    assert any("done 증거 없음" in c["body"] for c in got["comments"])


def test_close라벨은_done유지(client):
    i = client.post("/issues", json={"title": "close 예외", "labels": ["close"]}).json()
    client.post(f"/issues/{i['id']}/claim", json={"agent": "w@t"})
    r = client.patch(f"/issues/{i['id']}", json={"state": "done"}).json()
    assert r["state"] == "done" and r["verified"] == 1


class Recorder:
    def __init__(self, card):
        self.card = card
        self.patched = []
        self.verified = []
        self.commented = []

    def __call__(self, url, path, method="GET", body=None):
        if method == "GET":
            return dict(self.card)
        if path.endswith("/verify") and method == "POST":
            self.verified.append(body)
            return {**self.card, "state": "done"}
        if method == "PATCH":
            self.patched.append(body)
            return {**self.card, "state": body.get("state")}
        if method == "POST":
            self.commented.append(body)
            return {"ok": True}
        raise AssertionError(f"{method} {path}")


@pytest.fixture
def gh_ok(monkeypatch):
    monkeypatch.setattr(dispatchd, "gh_exec", lambda *a: "Merged")
    monkeypatch.setattr(dispatchd, "gh_json",
                        lambda *a: {"headRefOid": "a" * 40} if "view" in a else None)


def test_review카드_merge는_verify로_done(gh_ok, monkeypatch):
    card = {"id": "M3R7M0ZR-YF99", "state": "review", "version": 4,
            "completion_report": {"result": "passed"}}
    rec = Recorder(card)
    monkeypatch.setattr(dispatchd, "api", rec)
    dispatchd.execute("u", {"agent": "probe", "issue": card["id"], "action": "merge",
                            "pr": 7, "head_sha": "a" * 40})
    assert len(rec.verified) == 1 and rec.verified[0]["verifier"] == "probe"
    assert rec.verified[0]["completion_report"] == {"result": "passed"}
    assert not rec.patched


def test_review카드_리포트없으면_merge후_review유지(gh_ok, monkeypatch):
    card = {"id": "M3R7M0ZR-YF99", "state": "review", "version": 4, "completion_report": ""}
    rec = Recorder(card)
    monkeypatch.setattr(dispatchd, "api", rec)
    dispatchd.execute("u", {"agent": "probe", "issue": card["id"], "action": "merge",
                            "pr": 7, "head_sha": "a" * 40})
    assert not rec.verified and not rec.patched


def test_in_progress_merge는_무조건_review_정지(gh_ok, monkeypatch):
    card = {"id": "X-1", "state": "in_progress", "version": 2,
            "completion_report": {"result": "passed"}}
    rec = Recorder(card)
    monkeypatch.setattr(dispatchd, "api", rec)
    dispatchd.execute("u", {"agent": "probe", "issue": "X-1", "action": "merge",
                            "pr": 8, "head_sha": "a" * 40})
    assert rec.patched == [{"version": 2, "state": "review"}]
    assert not rec.verified


def _issue(state, iid="M3R7M0ZR-YF99", attempt=1, comments=()):
    return {"id": iid, "state": state, "assignee": "a@t", "lease_expires": None,
            "labels": [], "priority": None, "execution_attempt": attempt,
            "dispatches": 1, "release_ready": False, "waiting_for": None,
            "work_contract": {"version": "v2"}, "comments": list(comments), "title": iid}


def _snap(issues, prs=()):
    return {"auto": True, "now": "2026-10-01T10:00:00+0900", "agents": [],
            "issues": list(issues), "prs": list(prs)}


def test_review카드_PR없으면_needs_merge_판정():
    acts = dispatchd.decide(_snap([_issue("review")]))
    notes = [a for a in acts if a["action"] == "review-note"]
    assert len(notes) == 1 and "PR 없음" in notes[0]["reason"]
    assert notes[0]["issue"] == "M3R7M0ZR-YF99"


def test_needs_merge_회차_dedup():
    prior = [{"author": "probe", "body": "[needs-merge a1] PR 없음"}]
    acts = dispatchd.decide(_snap([_issue("review", comments=prior)]))
    assert not [a for a in acts if a["action"] == "review-note"]


def test_green_PR_있으면_review노트_안내고_merge_판정():
    pr = {"number": 9, "repo": "plainOldCode/think-tank",
          "checks": [{"state": "SUCCESS"}],
          "branch": "tt/M3R7M0ZR-YF99-x", "head_sha": "b" * 40}
    acts = dispatchd.decide(_snap([_issue("review")], [pr]))
    assert not [a for a in acts if a["action"] == "review-note"]
    assert [a for a in acts if a["action"] == "merge"]


def test_review노트_집행은_코멘트만(monkeypatch):
    rec = Recorder({"id": "X-2", "state": "review", "version": 1})
    monkeypatch.setattr(dispatchd, "api", rec)
    dispatchd.execute("u", {"agent": "probe", "issue": "X-2", "action": "review-note",
                            "reason": "PR 없음"})
    assert rec.commented and "needs-merge" in rec.commented[0]["body"]
    assert not rec.patched and not rec.verified
