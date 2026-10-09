"""probe 개선 (M4A4GV81-CCAV): 코멘트 PR URL 역기입·채택 + 스탈 공지.

HG5S 실측: 신규 repo PR + 카드 본문 repo: 미표기 → probe 스캔 풀 미진입 →
"PR 없음" 영구 정지. 탐지 가능한 정보(PR URL)는 이미 카드 코멘트에 있었다.
- A decide: 본문 repo: 없는 review 카드의 코멘트 PR URL을 파싱해 pr-adopt 액션
  발행 — needs-merge 마커와 독립(codex P1), 마커된 URL은 건너뛰고 다음 후보(codex P2)
- B execute pr-adopt: 채택 기록 코멘트만 — 본문 PATCH는 scope_changed로 보고를
  무효화하므로 하지 않는다(codex P1-2). 실제 풀 편입은 collect_repos가 코멘트에서 읽는다.
- C decide/execute: 'PR 없음' 24h 지속 review 카드를 메시지 보드에 공지(stale-notify)
"""
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

import probe.core
from app import create_app

IID = "M4A4GV81-CCAV"


def _issue(state="review", attempt=1, body="작업 설명", comments=(), updated_at="2026-10-07T09:00:00+0900"):
    return {"id": IID, "state": state, "assignee": "a@t", "version": 3, "lease_expires": None,
            "labels": [], "priority": None, "execution_attempt": attempt,
            "dispatches": 1, "release_ready": False, "waiting_for": None,
            "work_contract": {"version": "v2"}, "comments": list(comments),
            "updated_at": updated_at, "title": IID, "body": body}


def _snap(issues, prs=()):
    return {"auto": True, "now": "2026-10-07T10:00:00+0900", "agents": [],
            "issues": list(issues), "prs": list(prs)}


@pytest.fixture(autouse=True)
def grace_off(monkeypatch):
    monkeypatch.setenv("TT_REVIEW_GRACE_MIN", "0")


def _pr_comment(pr=7, repo="foo/bar"):
    return {"author": "a@t", "body": f"보고 완료 — https://github.com/{repo}/pull/{pr}"}


# --- decide: pr-adopt ---

def test_코멘트_PR_URL_있으면_pr_adopt_액션():
    acts = probe.core.decide(_snap([_issue(comments=[_pr_comment()])]))
    adopt = [a for a in acts if a["action"] == "pr-adopt"]
    assert len(adopt) == 1
    assert adopt[0]["repo"] == "foo/bar" and adopt[0]["pr"] == 7
    assert adopt[0]["issue"] == IID
    assert adopt[0]["marker"] == "[pr-adopted 7/foo/bar]"


def test_기존_needs_merge_마커_있어도_채택_판정된다():
    # P1: 마커 dedup이 채택을 차단하면 HG5S 복구(나중에 URL이 달리는 케이스)가 불가
    i = _issue(comments=[_pr_comment(),
                         {"author": "probe", "body": "[needs-merge a1] PR 없음 — 사람 판단 대기"}])
    assert [a for a in probe.core.decide(_snap([i])) if a["action"] == "pr-adopt"]


def test_채택_마커_있으면_재발행_없음():
    i = _issue(comments=[_pr_comment(),
                         {"author": "probe", "body": "[pr-adopted 7/foo/bar] 채택"}])
    assert not [a for a in probe.core.decide(_snap([i])) if a["action"] == "pr-adopt"]


def test_채택된_URL_다음_후보를_검사한다():
    # P2: 첫 URL이 이미 기록돼 있어도 뒤의 유효 후보를 채택
    i = _issue(comments=[_pr_comment(pr=7, repo="a/b"),
                         {"author": "probe", "body": "[pr-adopted 7/a/b] 채택"},
                         _pr_comment(pr=9, repo="c/d")])
    adopt = [a for a in probe.core.decide(_snap([i])) if a["action"] == "pr-adopt"]
    assert len(adopt) == 1 and adopt[0]["repo"] == "c/d" and adopt[0]["pr"] == 9


def test_본문에_repo_표기_있으면_미발행():
    i = _issue(body="작업 설명\n\nrepo: foo/bar", comments=[_pr_comment()])
    assert not [a for a in probe.core.decide(_snap([i])) if a["action"] == "pr-adopt"]


def test_collect_repos가_코멘트_PR_URL을_풀에_편입한다():
    i = _issue(comments=[_pr_comment(pr=7, repo="foo/bar")])
    assert "foo/bar" in probe.core.collect_repos([i])


def test_라운드트립_코멘트_URL의_PR이_관측되어_병합_후보가_된다(live, monkeypatch):
    # HG5S 재현→복구: 제출 카드 + 코멘트 PR URL(본문 repo: 없음) → 다음 사이클 관측
    iid = _new_issue(live)
    c = sqlite3.connect(live.db_path)
    c.execute("UPDATE issues SET execution_attempt=1, work_contract=?, state='review' WHERE id=?",
              ('{"report_required": true}', iid))
    c.commit(); c.close()
    live.post(f"/issues/{iid}/comments", json={"author": "a@t",
                                               "body": "보고 완료 — https://github.com/foo/bar/pull/7"})
    monkeypatch.setenv("TT_AUTO_DISPATCH", "1")
    monkeypatch.delenv("TT_REVIEW_AGENT", raising=False)
    snap = probe.core.snapshot("http://x")
    monkeypatch.setattr(probe.core, "collect_prs", lambda repos: [
        {"number": 7, "repo": "foo/bar", "branch": f"tt/{iid}", "title": "", "isDraft": False,
         "head_sha": "c" * 40, "checks": [{"state": "SUCCESS"}]}] if "foo/bar" in repos else [])
    snap["prs"] = probe.core.collect_prs(probe.core.collect_repos(snap["issues"]))
    acts = probe.core.decide(snap)
    assert any(a["action"] == "merge" for a in acts)


# --- decide: stale-notify (C) — 연령 기반('PR 없음' 노트는 attempt당 1회라 사이클 카운트 불가) ---

def _stale_notes(n):
    return [{"author": "probe", "body": f"[needs-merge a1] PR 없음 — 사람 판단 대기 #{k}"}
            for k in range(n)]


def test_PR_없음_스탈_기한_경과시_공지_액션():
    i = _issue(comments=_stale_notes(1),
               updated_at="2026-10-06T09:00:00+0900")  # 25h 경과
    acts = probe.core.decide(_snap([i]))
    st = [a for a in acts if a["action"] == "stale-notify"]
    assert len(st) == 1 and st[0]["marker"] == "[stale-notify a1]"


def test_PR_없음_스탈_기한_이내면_공지_없음():
    i = _issue(comments=_stale_notes(1))  # 1h 경과
    assert not [a for a in probe.core.decide(_snap([i])) if a["action"] == "stale-notify"]


def test_관측된_PR_있으면_스탈_공지_없다():
    # P2 회귀: needs-merge 마커 + 25h 경과여도 PR(PENDING/FAILURE/draft)이 보이면 공지 금지
    i = _issue(comments=_stale_notes(1), updated_at="2026-10-06T09:00:00+0900")
    pr = {"number": 7, "repo": "plainOldCode/think-tank", "branch": f"tt/{IID}",
          "title": "", "isDraft": False, "head_sha": "c" * 40, "checks": []}
    for variant in ({"checks": [{"state": "PENDING"}]}, {"checks": [{"state": "FAILURE"}]},
                    {"isDraft": True}):
        assert not [a for a in probe.core.decide(_snap([i], [{**pr, **variant}]))
                    if a["action"] == "stale-notify"]


# --- execute: pr-adopt / stale-notify ---

@pytest.fixture
def live(tmp_path, monkeypatch):
    c = TestClient(create_app(str(tmp_path / "p.db")))
    c.db_path = str(tmp_path / "p.db")
    monkeypatch.setattr(probe.core, "api", _make_api(c))
    return c


def _make_api(client):
    def api(url, path, method="GET", body=None):
        r = client.request(method, path, json=body)
        if r.status_code >= 400:
            raise RuntimeError(f"{method} {path} -> {r.status_code} {r.text[:200]}")
        return r.json() if r.text else None
    return api


def _new_issue(c, body="작업 설명"):
    r = c.post("/issues", json={"acceptance": "완료 기준: 테스트 통과", "title": IID, "body": body})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_pr_adopt_실행시_채택_코멘트만_기록(live):
    iid = _new_issue(live)
    probe.core.execute("http://x", {"action": "pr-adopt", "issue": iid, "repo": "foo/bar",
                                    "pr": 7, "marker": "[pr-adopted 7/foo/bar]"})
    got = live.get(f"/issues/{iid}").json()
    assert any("[pr-adopted 7/foo/bar]" in (c["body"] or "")
               for c in got["comments"] if c["author"] == "probe")
    assert (got["body"] or "") == "작업 설명"  # 본문 무변경


def test_pr_adopt는_보고와_회차를_무효화하지_않는다(live):
    # P1-2 회귀: 제출된 v2 보고 카드에 채택해도 attempt/보고가 그대로여야 한다
    iid = _new_issue(live)
    c = sqlite3.connect(live.db_path)
    report = json.dumps({"contract_version": "tt-tdd-v2.1:x", "attempt": 1, "result": "passed"})
    c.execute("UPDATE issues SET execution_attempt=1, work_contract=?, completion_report=?, "
              "state='review' WHERE id=?", ('{"report_required": true}', report, iid))
    c.commit(); c.close()
    probe.core.execute("http://x", {"action": "pr-adopt", "issue": iid, "repo": "foo/bar",
                                    "pr": 7, "marker": "[pr-adopted 7/foo/bar]"})
    got = live.get(f"/issues/{iid}").json()
    assert got["execution_attempt"] == 1
    rep = got["completion_report"]
    assert (json.loads(rep) if isinstance(rep, str) else rep)["result"] == "passed"
    assert got["state"] == "review"


def test_pr_adopt_마커_있으면_무음(live):
    iid = _new_issue(live)
    live.post(f"/issues/{iid}/comments", json={"author": "probe",
                                               "body": "[pr-adopted 7/foo/bar] 채택"})
    probe.core.execute("http://x", {"action": "pr-adopt", "issue": iid, "repo": "foo/bar",
                                    "pr": 7, "marker": "[pr-adopted 7/foo/bar]"})
    got = live.get(f"/issues/{iid}").json()
    assert len([c for c in got["comments"] if c["author"] == "probe"]) == 1


def test_stale_notify_실행시_보드_공지와_마커_코멘트(live):
    iid = _new_issue(live)
    live.patch(f"/issues/{iid}", json={"assignee": "b@t"})
    probe.core.execute("http://x", {"action": "stale-notify", "issue": iid,
                                    "marker": "[stale-notify a1]",
                                    "reason": "PR 없음 25시간 지속 — 사람 판단 대기 공지"})
    got = live.get(f"/issues/{iid}").json()
    assert any("[stale-notify a1]" in (c["body"] or "") for c in got["comments"])
    msgs = live.get("/messages").json()
    assert any("[stale]" in (m["body"] or "") for m in msgs)
