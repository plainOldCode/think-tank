"""ZDNH(M4TW2J3V) — 리뷰 승인 전 병합 차단: probe 승인 확정 시 tt/review-approved
commit status 게시. TT_REVIEW_STATUS=repo[,repo…] 가드 대상(미설정=off — 기존 동작).
게시 실패 시 병합 후보 제외 → ⓪b [needs-merge] dedup 규약으로 인간 판단 요청.

RED 기준: 전부 구현 전 실패한다.
"""
import pytest

import dispatchd
import probe.core


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


def _approved(i):
    i = dict(i)
    i["reviewer"] = "agy"
    i["version"] = 4
    i["comments"] = list(i.get("comments", [])) + [
        {"author": "agy", "body": "review: approve\nPR#9@" + "b" * 8}]
    return i


def test_승인_확정시_head에_status_게시(monkeypatch):
    monkeypatch.setenv("TT_REVIEW_AGENT", "agy")
    monkeypatch.setenv("TT_REVIEW_STATUS", "plainOldCode/think-tank")
    monkeypatch.setattr(probe.core, "review_status_posted", lambda repo, sha: "")
    calls = []
    monkeypatch.setattr(probe.core, "review_status_post",
                        lambda repo, sha, desc: calls.append((repo, sha, desc)))
    acts = dispatchd.decide(_snap([_approved(_issue("review"))], _pr([{"state": "SUCCESS"}])))
    assert [a for a in acts if a["action"] == "merge"]
    assert len(calls) == 1
    repo, sha, desc = calls[0]
    assert repo == "plainOldCode/think-tank" and sha == "b" * 40
    assert desc.startswith("TT#M3R7M0ZR-YF99")
    assert probe.core.REVIEW_STATUS_CTX == "tt/review-approved"


def test_이미_게시된_head는_재게시_없이_병합(monkeypatch):
    monkeypatch.setenv("TT_REVIEW_AGENT", "agy")
    monkeypatch.setenv("TT_REVIEW_STATUS", "plainOldCode/think-tank")
    monkeypatch.setattr(probe.core, "review_status_posted", lambda repo, sha: "success")
    calls = []
    monkeypatch.setattr(probe.core, "review_status_post",
                        lambda repo, sha, desc: calls.append((repo, sha, desc)))
    acts = dispatchd.decide(_snap([_approved(_issue("review"))], _pr([{"state": "SUCCESS"}])))
    assert [a for a in acts if a["action"] == "merge"]
    assert not calls


def test_게시_실패시_병합_보류_needsmerge_폴백(monkeypatch):
    monkeypatch.setenv("TT_REVIEW_AGENT", "agy")
    monkeypatch.setenv("TT_REVIEW_STATUS", "plainOldCode/think-tank")
    monkeypatch.setattr(probe.core, "review_status_posted", lambda repo, sha: "")

    def _boom(repo, sha, desc):
        raise RuntimeError("gh api 실패")
    monkeypatch.setattr(probe.core, "review_status_post", _boom)
    acts = dispatchd.decide(_snap([_approved(_issue("review"))], _pr([{"state": "SUCCESS"}])))
    assert not [a for a in acts if a["action"] == "merge"]
    notes = [a for a in acts if a["action"] == "review-note"]
    assert notes and "PR green" in notes[0]["reason"]


def test_리뷰_판정_없이는_게시하지_않는다(monkeypatch):
    monkeypatch.setenv("TT_REVIEW_STATUS", "plainOldCode/think-tank")
    monkeypatch.delenv("TT_REVIEW_AGENT", raising=False)
    calls = []
    monkeypatch.setattr(probe.core, "review_status_posted", lambda repo, sha: "")
    monkeypatch.setattr(probe.core, "review_status_post",
                        lambda repo, sha, desc: calls.append((repo, sha, desc)))
    acts = dispatchd.decide(_snap([_issue("review")], _pr([{"state": "SUCCESS"}])))
    assert [a for a in acts if a["action"] == "merge"]
    assert not calls


def test_request_changes에는_게시하지_않는다(monkeypatch):
    monkeypatch.setenv("TT_REVIEW_AGENT", "agy")
    monkeypatch.setenv("TT_REVIEW_STATUS", "plainOldCode/think-tank")
    calls = []
    monkeypatch.setattr(probe.core, "review_status_posted", lambda repo, sha: "")
    monkeypatch.setattr(probe.core, "review_status_post",
                        lambda repo, sha, desc: calls.append((repo, sha, desc)))
    i = dict(_approved(_issue("review")))
    i["comments"] = [{"author": "agy", "body": "review: request-changes\nPR#9@" + "b" * 8}]
    acts = dispatchd.decide(_snap([i], _pr([{"state": "SUCCESS"}])))
    assert [a for a in acts if a["action"] == "review-fix"]
    assert not [a for a in acts if a["action"] == "merge"]
    assert not calls


def test_가드_미설정은_게시_없이_기존_병합(monkeypatch):
    monkeypatch.setenv("TT_REVIEW_AGENT", "agy")
    monkeypatch.delenv("TT_REVIEW_STATUS", raising=False)
    calls = []
    monkeypatch.setattr(probe.core, "review_status_posted", lambda repo, sha: "")
    monkeypatch.setattr(probe.core, "review_status_post",
                        lambda repo, sha, desc: calls.append((repo, sha, desc)))
    acts = dispatchd.decide(_snap([_approved(_issue("review"))], _pr([{"state": "SUCCESS"}])))
    assert [a for a in acts if a["action"] == "merge"]
    assert not calls
