"""M59BE4HD-ME5W: STALL-kill fast-path — 죽은 리뷰어 재발령이 리스 만료를 기다리지 않는다."""
import sqlite3
from datetime import datetime, timedelta

import probe.core as core
from test_tt_lifecycle import snap, submitted


def review_round(client, tmp_path, issue, reviewer, killed=True, session_agent=None,
                 stale=False):
    """리뷰 라운드를 기록한다: claim-review + probe 발령 마커 + (선택) STALL-kill.

    stale=True면 kill을 마커들보다 앞으로 — 마지막 발령 이후의 죽음이 아닌 케이스."""
    base = f"/issues/{issue['id']}"
    assert client.post(base + "/claim-review", json={"agent": reviewer}).status_code == 200
    client.post(base + "/comments", json={
        "author": "probe", "body": f"[tt-review {issue['review_subject']}] reviewer={reviewer}"})
    if killed:
        client.post(base + "/comments", json={
            "author": "runner@m2max",
            "body": (f"runner:m2max dispatch#608 STALL-killed — 진행 신호 없음 "
                     f"session=tt-{session_agent or reviewer}-608")})
        # ts가 초 단위 해상도 — 마커와 kill의 선후를 확정한다(운영에선 분 단위 차이)
        con = sqlite3.connect(tmp_path / "tt.db")
        row = con.execute("SELECT ts FROM comments WHERE issue_id=? AND author='runner@m2max' "
                          "AND body LIKE '%STALL-killed%'", (issue["id"],)).fetchone()
        con.execute("UPDATE comments SET ts=? WHERE issue_id=? AND author='runner@m2max' "
                    "AND body LIKE '%STALL-killed%'",
                    ((datetime.fromisoformat(row[0])
                      + timedelta(seconds=(-60 if stale else 120))).isoformat(),
                     issue["id"]))
        con.commit()
        con.close()
    return client.get(base).json()


def kill_ts(issue):
    return next(c["ts"] for c in issue["comments"] if "STALL-killed" in (c.get("body") or ""))


def two_reviewers(s):
    s["agents"] = [{"name": "codex", "enabled": True, "receive_mode": "hook",
                    "base_url": "http://x/hook"},
                   {"name": "agy", "enabled": True, "receive_mode": "hook",
                    "base_url": "http://y/hook"}]
    return s


def test_stall_kill_redispatches_before_lease_expiry(client, tmp_path):
    issue = review_round(client, tmp_path, submitted(client), "codex")
    s = two_reviewers(snap(issue))
    s["now"] = (datetime.fromisoformat(kill_ts(issue)) + timedelta(minutes=12)).isoformat()
    acts = core.decide(s)
    assert [a["action"] for a in acts] == ["tt-review"]
    assert acts[0]["agent"] != "codex"
    assert acts[0].get("release_lease") == "codex"
    assert acts[0].get("retry") == 1


def test_stall_kill_within_grace_waits(client, tmp_path):
    issue = review_round(client, tmp_path, submitted(client), "codex")
    s = two_reviewers(snap(issue))
    s["now"] = (datetime.fromisoformat(kill_ts(issue)) + timedelta(minutes=3)).isoformat()
    assert not any(a["action"] == "tt-review" for a in core.decide(s))


def test_stale_kill_comment_ignored(client, tmp_path):
    """발령 마커가 kill 코멘트보다 뒤에 있으면(새 라운드) 옛 kill은 무시한다."""
    issue = review_round(client, tmp_path, submitted(client), "codex", stale=True)
    s = two_reviewers(snap(issue))
    s["now"] = (datetime.fromisoformat(kill_ts(issue)) + timedelta(minutes=12)).isoformat()
    assert not any(a["action"] == "tt-review" for a in core.decide(s))


def test_kill_of_other_agent_ignored(client, tmp_path):
    issue = review_round(client, tmp_path, submitted(client), "agy", session_agent="codex")
    s = two_reviewers(snap(issue))
    s["now"] = (datetime.fromisoformat(kill_ts(issue)) + timedelta(minutes=12)).isoformat()
    assert not any(a["action"] == "tt-review" for a in core.decide(s))


def _api(client):
    def api(url, path, method="GET", body=None):
        r = client.request(method, path, json=body)
        assert r.status_code < 400, r.text
        return r.json()
    return api


def test_execute_releases_dead_lease_and_claims_next_reviewer(client, monkeypatch, tmp_path):
    issue = review_round(client, tmp_path, submitted(client), "codex")
    assert client.post("/agents", json={"name": "agy", "receive_mode": "hook",
                                        "base_url": "http://y/hook"}).status_code in (200, 201)
    s = two_reviewers(snap(issue))
    s["now"] = (datetime.fromisoformat(kill_ts(issue)) + timedelta(minutes=12)).isoformat()
    monkeypatch.setattr(core, "api", _api(client))
    act = core.decide(s)[0]
    core.execute("http://tt", act)
    got = client.get(f"/issues/{issue['id']}").json()
    assert got["reviewer"] == "agy"
    assert got["lease_by"] == "agy"
    lease = (datetime.fromisoformat(got["lease_expires"])
             - datetime.fromisoformat(got["updated_at"])).total_seconds()
    assert lease <= 3700  # probe 발령 리뷰 리스 1h(기본 2h 단축)
    assert any(d["agent"] == "agy"
               for d in client.get(f"/issues/{issue['id']}/dispatches").json())
