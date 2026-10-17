"""12MZ reviewer_session_failed — 판정 없는 리뷰 정체 감지·자동 재발령.

발령 이력([review-req #pr/sha8] 마커)이 있는 head에 판정 코멘트가 없고 리뷰어
점유 흔적(release되지 않은 reviewer) 또는 runner 실패 코멘트가 있으면 세션 사망
정체로 판정한다. 회차 마커 [review-retry a<n> #pr/sha8]로 dedup — probe의
review-req 마커 dedup과 충돌하지 않는다. (사유,pr,sha) 카운터 N=3 도달 시
needs-human 에스컬레이션 1회, head 변경 시 마커가 sha8을 달리 해 카운터 자동 리셋.
"""
import pytest

import dispatchd

SHA8 = "b" * 8
NOW = "2026-10-09T10:00:00+0900"
OLD = "2026-10-09T09:00:00+0900"  # NOW-1h — TT_REVIEW_STALL_MIN=0에서 항상 경과


def _i(**kw):
    base = {"id": "M4ABCDEF-GH12", "state": "review", "archived": 0, "version": 3,
            "assignee": "a1@t",
            "reviewer": "", "lease_by": "", "lease_expires": None, "labels": [],
            "dispatches": 1, "execution_attempt": 1,
            "work_contract": {"version": "v2"}, "comments": [],
            "updated_at": "2026-10-09T09:00:00+0900", "title": "M4ABCDEF-GH12"}
    base.update(kw)
    return base


def _snap(issues, prs=(), agents=()):
    return {"auto": True, "now": NOW, "agents": list(agents),
            "issues": list(issues), "prs": list(prs)}


def _pr(number=9, sha="b" * 40):
    return [{"number": number, "repo": "plainOldCode/think-tank",
             "checks": [{"state": "SUCCESS"}],
             "branch": "tt/M4ABCDEF-GH12-x", "title": "", "head_sha": sha}]


def _agent(name):
    return {"name": name, "enabled": True, "base_url": "http://x", "receive_mode": "hook"}


def _pc(body, ts=OLD, author="probe"):
    # 실측 wire 형식(db.comments_of): {author, body, ts} — created_at 아님(리뷰 R1)
    return {"author": author, "body": body, "ts": ts}


REQ = (f"[review-req #9/{SHA8}] PR#9 CI green — 리뷰 요청 → fallback@t (dispatch#294)")
RUNNER_FAIL = {"author": "runner@m2max", "ts": NOW,  # 실측 author 형식(리뷰 R2)
               "body": "runner:m2max dispatch#294 failed exit=1 session=tt-codex-294\nReconnecting 5/5"}
RUNNER_OK = {"author": "runner@m2max", "ts": NOW,
             "body": "runner:m2max dispatch#294 done exit=0 session=tt-codex-294"}


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("TT_REVIEW_GRACE_MIN", "0")
    monkeypatch.setenv("TT_REVIEW_STALL_MIN", "0")
    monkeypatch.setenv("TT_REVIEW_AGENT", "fallback@t")


def test_세션사망_정체에서_1회_재발령한다():
    i = _i(reviewer="fallback@t", comments=[_pc(REQ)])
    acts = dispatchd.decide(_snap([i], _pr(), [_agent("a2@t")]))
    retries = [a for a in acts if a["action"] == "review-retry"]
    assert len(retries) == 1
    assert retries[0]["marker"] == f"[review-retry a1 #9/{SHA8}]"
    assert retries[0]["round"] == 1
    assert "새 세션" in retries[0]["reason"]
    # dedup 경로 치환 확인 — 기존 review-request가 조용히 스킵되던 자리
    assert not [a for a in acts if a["action"] == "review-request"]


def test_회차가_증가하고_상한_도달시_1회만_에스컬레이션한다():
    i2 = _i(reviewer="fallback@t", comments=[_pc(REQ), _pc(f"[review-retry a1 #9/{SHA8}] 재발령")])
    acts = dispatchd.decide(_snap([i2], _pr(), [_agent("a2@t")]))
    assert [a["marker"] for a in acts if a["action"] == "review-retry"] == [f"[review-retry a2 #9/{SHA8}]"]

    i3 = _i(reviewer="fallback@t",
            comments=[_pc(REQ), _pc(f"[review-retry a1 #9/{SHA8}]"),
                      _pc(f"[review-retry a2 #9/{SHA8}]")])
    acts = dispatchd.decide(_snap([i3], _pr(), [_agent("a2@t")]))
    assert [a["marker"] for a in acts if a["action"] == "review-retry"] == [f"[review-retry a3 #9/{SHA8}]"]

    i4 = _i(reviewer="fallback@t",
            comments=[_pc(REQ), _pc(f"[review-retry a1 #9/{SHA8}]"),
                      _pc(f"[review-retry a2 #9/{SHA8}]"), _pc(f"[review-retry a3 #9/{SHA8}]")])
    acts = dispatchd.decide(_snap([i4], _pr(), [_agent("a2@t")]))
    assert not [a for a in acts if a["action"] == "review-retry"]
    nh = [a for a in acts if a["action"] == "needs-human"]
    assert len(nh) == 1 and "판정 없음" in nh[0]["note"]

    # 에스컬레이션 dedup — 실제 executor(author=dispatchd) 출력 형식으로도 무반복(리뷰 R6)
    i5 = _i(reviewer="fallback@t",
            comments=[_pc(REQ), _pc(f"[review-retry a3 #9/{SHA8}]"),
                      _pc(f"[needs-human-review #9/{SHA8}] 사람 판단 대기", author="dispatchd")])
    acts = dispatchd.decide(_snap([i5], _pr(), [_agent("a2@t")]))
    assert not [a for a in acts if a["action"] == "needs-human"]


def test_정상_판정이_있으면_재발령하지_않는다():
    i = _i(reviewer="fallback@t",
           comments=[_pc(REQ),
                     {"author": "fallback@t", "created_at": OLD,
                      "body": "review: approve\nPR#9@bbbbbbbb — ok"}])
    acts = dispatchd.decide(_snap([i], _pr(), [_agent("a2@t")]))
    assert not [a for a in acts if a["action"] == "review-retry"]
    assert [a for a in acts if a["action"] == "merge"]


def test_head_변경시_카운터가_리셋된다():
    # 구 head 마커만 존재 — 새 head(cccc)에는 발령 이력 없음 → 정체 판정 대상 아님
    i = _i(reviewer="fallback@t",
           comments=[_pc(f"[review-req #9/aaaaaaaa] 리뷰 요청"),
                     _pc(f"[review-retry a3 #9/aaaaaaaa]")])
    acts = dispatchd.decide(_snap([i], _pr(sha="c" * 40), [_agent("a2@t")]))
    assert not [a for a in acts if a["action"] == "review-retry"]
    assert not [a for a in acts if a["action"] == "needs-human"]


def test_runner_실패_코멘트는_관망_창_내라도_즉시_트리거한다():
    i = _i(reviewer="fallback@t",
           comments=[_pc(REQ, ts=NOW),  # 방금 발령 — 관망 창 내
                     dict(RUNNER_FAIL)])
    acts = dispatchd.decide(_snap([i], _pr(), [_agent("a2@t")]))
    assert [a["marker"] for a in acts if a["action"] == "review-retry"] == [f"[review-retry a1 #9/{SHA8}]"]


def test_runner_성공_코멘트는_트리거가_아니다(monkeypatch):
    # 관망 창 30분 — 마커는 방금(창 내), runner 코멘트는 성공(exit=0)
    monkeypatch.setattr("probe.core._stall_window_min", lambda: 30)
    i = _i(reviewer="fallback@t",
           comments=[_pc(REQ, ts=NOW), dict(RUNNER_OK)])
    acts = dispatchd.decide(_snap([i], _pr(), [_agent("a2@t")]))
    assert not [a for a in acts if a["action"] == "review-retry"]
    assert not [a for a in acts if a["action"] == "needs-human"]


def test_최근_발령은_관망한다(monkeypatch):
    monkeypatch.setattr("probe.core._stall_window_min", lambda: 30)
    i = _i(reviewer="fallback@t", comments=[_pc(REQ, ts=NOW)])
    acts = dispatchd.decide(_snap([i], _pr(), [_agent("a2@t")]))
    assert not [a for a in acts if a["action"] == "review-retry"]
    assert not [a for a in acts if a["action"] == "review-request"]


def test_구_실패는_새_세션과_새_head의_관망을_깨지_않는다(monkeypatch):
    # 리뷰 R3 — 실패 코멘트가 최신 발령 마커보다 오래되면 즉시 트리거가 아니다
    monkeypatch.setattr("probe.core._stall_window_min", lambda: 30)
    # 실패(ts=OLD) → 재발령 a1(ts=NOW): 실패 < 최신 마커 → 즉시 트리거 아님(관망)
    i = _i(reviewer="fallback@t",
           comments=[_pc(REQ, ts=OLD), dict(RUNNER_FAIL, ts=OLD),
                     _pc(f"[review-retry a1 #9/{SHA8}]", ts=NOW)])
    acts = dispatchd.decide(_snap([i], _pr(), [_agent("a2@t")]))
    assert not [a for a in acts if a["action"] == "review-retry"]
    # 구 head(cccc) 실패 + 새 head(bbbb) 발령 → 새 head에선 정상 요청 경로(재발령 아님)
    i2 = _i(reviewer="fallback@t",
            comments=[_pc(f"[review-req #9/cccccccc] 리뷰 요청"),
                      {"author": "runner@m2max", "ts": OLD,
                       "body": "runner:m2max dispatch#280 failed exit=1 session=x"}])
    acts = dispatchd.decide(_snap([i2], _pr(sha="b" * 40), [_agent("a2@t")]))
    assert not [a for a in acts if a["action"] == "review-retry"]


def test_마지막_재발령도_관망_창을_받는다(monkeypatch):
    # 리뷰 R7 — a3 직후에는 에스컬레이션하지 않는다(정체 판정이 먼저)
    monkeypatch.setattr("probe.core._stall_window_min", lambda: 30)
    i = _i(reviewer="fallback@t",
           comments=[_pc(REQ, ts=NOW), _pc(f"[review-retry a3 #9/{SHA8}]", ts=NOW)])
    acts = dispatchd.decide(_snap([i], _pr(), [_agent("a2@t")]))
    assert not [a for a in acts if a["action"] == "needs-human"]
    assert not [a for a in acts if a["action"] == "review-retry"]


def test_execute_review_retry는_회차_마커로_재발령한다(monkeypatch):
    import probe.core

    calls = []

    def fake_api(url, path, method="GET", body=None):
        calls.append((method, path, body))
        if path.endswith("/claim-review"):
            return {"id": "M4ABCDEF-GH12"}
        return {"comments": [], "version": 3, "assignee": "a1@t"}

    monkeypatch.setattr(probe.core, "api", fake_api)
    marker = f"[review-retry a1 #9/{SHA8}]"
    probe.core.execute("http://x", {"action": "review-retry", "agent": "probe",
                                    "issue": "M4ABCDEF-GH12", "pr": 9, "repo": "o/r",
                                    "branch": "tt/x", "head_sha": "b" * 40,
                                    "marker": marker, "round": 1,
                                    "reviewer": "fallback@t", "reason": "정체"})
    disp = [b for m, p, b in calls if p.endswith("/dispatch") and m == "POST"]
    assert len(disp) == 1
    assert disp[0]["agent"] == "fallback@t"
    assert "새 세션에서 시작" in disp[0]["message"] and "재사용 금지" in disp[0]["message"]
    # 리뷰 R4: 회차 마커가 본문에 들어가 멱등키가 회차를 구분한다
    assert marker in disp[0]["message"]
    # 리뷰 R5: 이전 세션 context 상속을 끊는다(fresh)
    assert disp[0]["fresh"] is True
    # 회차 마커 코멘트 영속화
    comments = [b for m, p, b in calls if p.endswith("/comments") and m == "POST"]
    assert any(marker in (b.get("body") or "") for b in comments)


def test_fresh_dispatch는_context를_상속하지_않는다(client):
    # 리뷰 R5 서버 경로 — fresh=True면 이전 회차 resume context를 끊는다
    import json as _json
    with client as c:
        c.post("/agents", json={"name": "r@t", "base_url": "http://hook/x"})
        r = c.post("/issues", json={"id": "M4FRESH-TEST-01", "title": "t",
                                    "acceptance": "x"})
        iid = r.json()["id"]
        # 1회차 행을 만든 뒤 context 주입(웹훅 응답 상속을 흉내)
        import sqlite3 as _sq
        c.post(f"/issues/{iid}/dispatch", json={"agent": "r@t", "message": "1차 지시"})
        con = _sq.connect(client.app.state.ctx.db_path)
        con.execute("UPDATE dispatches SET context=? WHERE issue_id=?",
                    ("runner:m2max:tmux:tt-old-1", iid))
        con.commit()
        con.close()
        # 일반 dispatch는 상속, fresh dispatch는 끊음 — 행의 context 컬럼으로 검증:
        # fresh는 INSERT 시 inh_ctx=""(전달 실패여도 유지)
        c.post(f"/issues/{iid}/dispatch", json={"agent": "r@t", "message": "일반 지시"})
        c.post(f"/issues/{iid}/dispatch", json={"agent": "r@t", "message": "재발령 지시",
                                                "fresh": True})
        con = _sq.connect(client.app.state.ctx.db_path)
        rows = con.execute("SELECT context FROM dispatches WHERE issue_id=? ORDER BY id",
                           (iid,)).fetchall()
        con.close()
        assert rows[1][0] == "runner:m2max:tmux:tt-old-1"
        assert rows[2][0] == ""


def test_실패_dispatch_id가_발령과_다르면_즉시_트리거가_아니다(monkeypatch):
    # 리뷰 3차 R3 잔여 — 구 회차·작업 dispatch의 늦은/동초 실패는 새 세션을 깨지 않는다
    monkeypatch.setattr("probe.core._stall_window_min", lambda: 30)
    # 발령(dispatch#300) 후 구 회차 실패(dispatch#294)가 늦게 접수 → 관망
    i = _i(reviewer="fallback@t",
           comments=[_pc(REQ.replace("dispatch#294", "dispatch#300"), ts=NOW),
                     dict(RUNNER_FAIL, ts=NOW)])  # 동초 접수라도 id 불일치
    acts = dispatchd.decide(_snap([i], _pr(), [_agent("a2@t")]))
    assert not [a for a in acts if a["action"] == "review-retry"]


def test_발령_마커에_dispatch_id가_없으면_실패는_즉시_트리거가_아니다(monkeypatch):
    # 구형 마커(dispatch id 미기록) — 창 경과로만 판정(보수적)
    monkeypatch.setattr("probe.core._stall_window_min", lambda: 30)
    i = _i(reviewer="fallback@t",
           comments=[_pc(REQ.split(" (dispatch")[0], ts=NOW), dict(RUNNER_FAIL)])
    acts = dispatchd.decide(_snap([i], _pr(), [_agent("a2@t")]))
    assert not [a for a in acts if a["action"] == "review-retry"]


def test_execute_review_request는_마커에_dispatch_id를_남긴다(monkeypatch):
    import probe.core

    calls = []

    def fake_api(url, path, method="GET", body=None):
        calls.append((method, path, body))
        if path.endswith("/claim-review"):
            return {"id": "M4ABCDEF-GH12"}
        if path.endswith("/dispatch") and method == "POST":
            return {"id": 777}
        return {"comments": [], "version": 3, "assignee": "a1@t"}

    monkeypatch.setattr(probe.core, "api", fake_api)
    probe.core.execute("http://x", {"action": "review-request", "agent": "probe",
                                    "issue": "M4ABCDEF-GH12", "pr": 9, "repo": "o/r",
                                    "branch": "tt/x", "head_sha": "b" * 40,
                                    "marker": f"[review-req #9/{SHA8}]"})
    comments = [b for m, p, b in calls if p.endswith("/comments") and m == "POST"]
    assert any("(dispatch#777)" in (b.get("body") or "") for b in comments)


def test_동초_마커는_댓글_id로_최신을_선택한다(monkeypatch):
    # 리뷰 4차 R3 — 동초(req+retry)에서 첫 행의 구 dispatch id로 고정되면 현재 실패를 놓침
    monkeypatch.setattr("probe.core._stall_window_min", lambda: 30)
    i = _i(reviewer="fallback@t",
           comments=[_pc(REQ.replace("dispatch#294", "dispatch#290"), ts=NOW),
                     _pc(REQ.replace("[review-req", "[review-retry a1").replace(
                         "dispatch#294", "dispatch#300"), ts=NOW),
                     dict(RUNNER_FAIL)])  # dispatch#294 실패 — a1(300)과 불일치 → 무트리거
    i["comments"][0]["id"], i["comments"][1]["id"] = 10, 11
    acts = dispatchd.decide(_snap([i], _pr(), [_agent("a2@t")]))
    assert not [a for a in acts if a["action"] == "review-retry"]
    # 실패 id가 최신 마커(300)와 일치하면 즉시 트리거
    i2 = _i(reviewer="fallback@t",
            comments=[_pc(REQ.replace("dispatch#294", "dispatch#290"), ts=NOW),
                      _pc(REQ.replace("[review-req", "[review-retry a1").replace(
                          "dispatch#294", "dispatch#300"), ts=NOW),
                      {"author": "runner@m2max", "ts": NOW,
                       "body": "runner:m2max dispatch#300 failed exit=1 session=s"}])
    i2["comments"][0]["id"], i2["comments"][1]["id"] = 10, 11
    acts = dispatchd.decide(_snap([i2], _pr(), [_agent("a2@t")]))
    assert [a["marker"] for a in acts if a["action"] == "review-retry"] == [f"[review-retry a2 #9/{SHA8}]"]
