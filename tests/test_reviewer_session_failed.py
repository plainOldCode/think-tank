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
    return {"name": name, "enabled": True, "base_url": "http://x"}


def _pc(body, ts=OLD):
    return {"author": "probe", "body": body, "created_at": ts}


REQ = f"[review-req #9/{SHA8}] PR#9 CI green — 리뷰 요청 → fallback@t"


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

    # 에스컬레이션 dedup — needs-human-review 마커가 이미 있으면 반복 없음
    i5 = _i(reviewer="fallback@t",
            comments=[_pc(REQ), _pc(f"[review-retry a3 #9/{SHA8}]"),
                      _pc(f"[needs-human-review #9/{SHA8}] 사람 판단 대기")])
    acts = dispatchd.decide(_snap([i5], _pr(), [_agent("a2@t")]))
    assert not [a for a in acts if a["action"] == "needs-human"]


def test_정상_판정이_있으면_재발령하지_않는다():
    i = _i(reviewer="fallback@t",
           comments=[_pc(REQ),
                     {"author": "fallback@t", "created_at": OLD,
                      "body": "review: approve PR#9@bbbbbbbb — ok"}])
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
                     {"author": "runner:m2max", "created_at": NOW,
                      "body": "runner:m2max dispatch#294 failed exit=1 session=tt-codex-294\nReconnecting 5/5"}])
    acts = dispatchd.decide(_snap([i], _pr(), [_agent("a2@t")]))
    assert [a["marker"] for a in acts if a["action"] == "review-retry"] == [f"[review-retry a1 #9/{SHA8}]"]


def test_runner_성공_코멘트는_트리거가_아니다(monkeypatch):
    # 관망 창 30분 — 마커는 방금(창 내), runner 코멘트는 성공(exit=0)
    monkeypatch.setattr("probe.core._stall_window_min", lambda: 30)
    i = _i(reviewer="fallback@t",
           comments=[_pc(REQ, ts=NOW),
                     {"author": "runner:m2max", "created_at": NOW,
                      "body": "runner:m2max dispatch#294 done exit=0 session=tt-codex-294"}])
    acts = dispatchd.decide(_snap([i], _pr(), [_agent("a2@t")]))
    assert not [a for a in acts if a["action"] == "review-retry"]
    assert not [a for a in acts if a["action"] == "needs-human"]


def test_최근_발령은_관망한다(monkeypatch):
    monkeypatch.setattr("probe.core._stall_window_min", lambda: 30)
    i = _i(reviewer="fallback@t", comments=[_pc(REQ, ts=NOW)])
    acts = dispatchd.decide(_snap([i], _pr(), [_agent("a2@t")]))
    assert not [a for a in acts if a["action"] == "review-retry"]
    assert not [a for a in acts if a["action"] == "review-request"]


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
    # 회차 마커 코멘트 영속화
    comments = [b for m, p, b in calls if p.endswith("/comments") and m == "POST"]
    assert any(marker in (b.get("body") or "") for b in comments)
