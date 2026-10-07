"""probe 개선 (M4A4GV81-CCAV): 코멘트 PR URL 역기입·채택 + 스탈 공지.

HG5S 실측: 신규 repo PR + 카드 본문 repo: 미표기 → probe 스캔 풀 미진입 →
"PR 없음" 영구 정지. 탐지 가능한 정보(PR URL)는 이미 카드 코멘트에 있었다.
- A decide: 본문 repo: 없는 review 카드가 "PR 없음" 판정 지점에서 코멘트 PR URL을
  파싱해 pr-adopt 액션 발행(마커 dedup)
- B execute pr-adopt: gh로 PR 실재·카드 ID 일치 검증 후 본문 repo: 보강(무음 멱등)
- C decide/execute: 'PR 없음' N회차 지속 review 카드를 메시지 보드에 공지(stale-notify)
"""
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


def test_채택_마커_있으면_재발행_없음():
    i = _issue(comments=[_pr_comment(),
                         {"author": "probe", "body": "[pr-adopted 7/foo/bar] 채택"}])
    assert not [a for a in probe.core.decide(_snap([i])) if a["action"] == "pr-adopt"]


def test_본문에_repo_표기_있으면_미발행():
    i = _issue(body="작업 설명\n\nrepo: foo/bar", comments=[_pr_comment()])
    assert not [a for a in probe.core.decide(_snap([i])) if a["action"] == "pr-adopt"]


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


# --- execute: pr-adopt / stale-notify ---

@pytest.fixture
def live(tmp_path, monkeypatch):
    c = TestClient(create_app(str(tmp_path / "p.db")))
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
    r = c.post("/issues", json={"title": IID, "body": body})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_pr_adopt_실행시_본문_보강과_채택_코멘트(live, monkeypatch):
    iid = _new_issue(live)
    monkeypatch.setattr(probe.core, "gh_json",
                        lambda *a: {"title": f"tt/{iid}: x", "headRefName": f"tt/{iid}"})
    probe.core.execute("http://x", {"action": "pr-adopt", "issue": iid, "repo": "foo/bar",
                                    "pr": 7, "marker": "[pr-adopted 7/foo/bar]"})
    got = live.get(f"/issues/{iid}").json()
    assert "repo: foo/bar" in (got["body"] or "")
    assert any("채택" in (c["body"] or "") for c in got["comments"] if c["author"] == "probe")


def test_pr_adopt_카드_ID_불일치_PR은_스킵(live, monkeypatch):
    iid = _new_issue(live)
    monkeypatch.setattr(probe.core, "gh_json", lambda *a: {"title": "다른 카드 작업",
                                                           "headRefName": "feature/other"})
    probe.core.execute("http://x", {"action": "pr-adopt", "issue": iid, "repo": "foo/bar",
                                    "pr": 7, "marker": "[pr-adopted 7/foo/bar]"})
    got = live.get(f"/issues/{iid}").json()
    assert "repo: foo/bar" not in (got["body"] or "")
    assert not got["comments"]


def test_pr_adopt_이미_본문_표기시_무음(live, monkeypatch):
    iid = _new_issue(live, body="작업 설명\n\nrepo: foo/bar")
    monkeypatch.setattr(probe.core, "gh_json",
                        lambda *a: {"title": f"tt/{iid}: x", "headRefName": f"tt/{iid}"})
    probe.core.execute("http://x", {"action": "pr-adopt", "issue": iid, "repo": "foo/bar",
                                    "pr": 7, "marker": "[pr-adopted 7/foo/bar]"})
    got = live.get(f"/issues/{iid}").json()
    assert not any("채택" in (c["body"] or "") for c in got["comments"])


def test_stale_notify_실행시_보드_공지와_마커_코멘트(live):
    iid = _new_issue(live)
    live.patch(f"/issues/{iid}", json={"assignee": "b@t"})
    probe.core.execute("http://x", {"action": "stale-notify", "issue": iid,
                                    "marker": "[stale-notify a1]",
                                    "reason": "PR 없음 3회차 지속 — 사람 판단 대기 공지"})
    got = live.get(f"/issues/{iid}").json()
    assert any("[stale-notify a1]" in (c["body"] or "") for c in got["comments"])
    msgs = live.get("/messages").json()
    assert any("[stale]" in (m["body"] or "") for m in msgs)
