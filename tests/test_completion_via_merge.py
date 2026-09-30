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


def _issue(state, iid="M3R7M0ZR-YF99", attempt=1, comments=(), updated="2026-10-01T09:00:00+0900"):
    return {"id": iid, "state": state, "assignee": "a@t", "lease_expires": None,
            "labels": [], "priority": None, "execution_attempt": attempt,
            "dispatches": 1, "release_ready": False, "waiting_for": None,
            "work_contract": {"version": "v2"}, "comments": list(comments),
            "updated_at": updated, "title": iid}


def _snap(issues, prs=()):
    return {"auto": True, "now": "2026-10-01T10:00:00+0900", "agents": [],
            "issues": list(issues), "prs": list(prs)}


def _pr(checks, branch="tt/M3R7M0ZR-YF99-x", title=""):
    return [{"number": 9, "repo": "plainOldCode/think-tank", "checks": checks,
             "branch": branch, "title": title, "head_sha": "b" * 40}]


def test_PR_제목_ID만_있어도_카드_조인_merge_판정():
    # 재작업으로 브랜치명이 규약 밖(fix/...)이어도 제목의 카드 ID로 조인
    i = _issue("review", updated="2026-10-01T08:00:00+0900")
    prs = _pr([{"state": "SUCCESS"}], branch="fix/retry-work",
              title="재작업: review-note 유예 수정 (M3R7M0ZR-YF99)")
    acts = dispatchd.decide(_snap([i], prs))
    merges = [a for a in acts if a["action"] == "merge"]
    assert len(merges) == 1 and merges[0]["issue"] == "M3R7M0ZR-YF99"
    assert not [a for a in acts if a["action"] == "review-note"]


def test_armour라벨_repo_매핑과_review카드_repo_수집():
    i = {"id": "M3S0PAK0-GBE5", "state": "review", "assignee": "a@t", "lease_expires": None,
         "labels": ["armour"], "priority": None, "execution_attempt": 1, "dispatches": 1,
         "release_ready": False, "waiting_for": None, "work_contract": {"version": "v2"},
         "comments": [], "updated_at": "2026-10-01T09:00:00+0900", "title": "wal busy"}
    assert dispatchd.card_repo(i) == "plainOldCode/armour-service-ops"
    assert "plainOldCode/armour-service-ops" in dispatchd.collect_repos([i])


def test_알려진_repo는_항시_수집대상():
    # 카드 repo 표기 없어도 known repo 전체는 매 라운드 스캔 대상 (사용자 지시)
    repos = dispatchd.collect_repos([])
    assert "plainOldCode/think-tank" in repos
    assert "plainOldCode/armour-service-ops" in repos


def _issue_repoless(**kw):
    i = _issue("review", updated="2026-10-01T08:00:00+0900", **kw)
    return i


def test_tt접두어외_브랜치도_ID검출_merge():
    # hermes/M...-... 등 재작업 접두어 — 브랜치 전체에서 카드 ID 스캔
    i = _issue_repoless()
    prs = _pr([{"state": "SUCCESS"}], branch="hermes/M3R7M0ZR-YF99-retry", title="retry")
    acts = dispatchd.decide(_snap([i], prs))
    assert [a for a in acts if a["action"] == "merge"]


def test_repo미상카드는_발견_PR_repo_추론으로_merge():
    i = _issue_repoless()
    i["labels"] = []  # card_repo=None (기본 이슈엔 labels [])
    prs = _pr([{"state": "SUCCESS"}], branch="fix/wal", title="M3R7M0ZR-YF99 수정")
    prs[0]["repo"] = "plainOldCode/armour-service-ops"
    acts = dispatchd.decide(_snap([i], prs))
    merges = [a for a in acts if a["action"] == "merge"]
    assert len(merges) == 1 and merges[0]["repo"] == "plainOldCode/armour-service-ops"


def test_PR_그냥_ID는_무관카드_pr_card_id_무시():
    # 다른 카드 ID 브랜치 + 본 카드 제목 없음 → 본 카드엔 PR 없음 판정(유예 경과 후)
    i = _issue("review", updated="2026-10-01T08:00:00+0900")
    prs = _pr([{"state": "SUCCESS"}], branch="tt/M9ZZZZZZ-AAAA-x",
              title="무관 작업")
    acts = dispatchd.decide(_snap([i], prs))
    notes = [a for a in acts if a["action"] == "review-note"]
    assert len(notes) == 1 and "PR 없음" in notes[0]["reason"]


def test_review카드_PR없으면_needs_merge_판정():
    acts = dispatchd.decide(_snap([_issue("review")]))
    notes = [a for a in acts if a["action"] == "review-note"]
    assert len(notes) == 1 and "PR 없음" in notes[0]["reason"]
    assert notes[0]["issue"] == "M3R7M0ZR-YF99"


def test_review직후_PR없으면_유예_코멘트안한다():
    # review 전이(updated) 5분 전 — PR 미생성 여유 구간 (M3RXY7KZ 관측: 성급 판정)
    i = _issue("review", updated="2026-10-01T09:55:00+0900")
    acts = dispatchd.decide(_snap([i]))
    assert not [a for a in acts if a["action"] == "review-note"]


def test_CI_진행중이면_코멘트안한다_재확인만():
    i = _issue("review", updated="2026-10-01T08:00:00+0900")
    acts = dispatchd.decide(_snap([i], _pr([{"state": "PENDING"}, {"state": "SUCCESS"}])))
    assert not [a for a in acts if a["action"] in ("review-note", "merge")]


def test_CI_명시실패는_즉시_needs_merge():
    i = _issue("review", updated="2026-10-01T09:58:00+0900")
    acts = dispatchd.decide(_snap([i], _pr([{"state": "FAILURE"}])))
    notes = [a for a in acts if a["action"] == "review-note"]
    assert len(notes) == 1 and "CI 실패" in notes[0]["reason"]


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
