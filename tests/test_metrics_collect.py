"""TT 개선#3e (M4FFCB9J-2AAB): 지표 수집기 회귀 테스트.

scripts/metrics_collect.py는 독립 CLI(네트워크 없이 collect() 호출 가능)라 합성
이벤트로 창 경계·페어링·코호트 동작을 잠근다. 독립 리뷰(PR#66@45eb208·69e16dd·
351f2ca7)에서 지적된 경계들이 회귀하지 않게 하는 게 목적이다.
"""
import importlib.util
import itertools
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_script = Path(__file__).resolve().parents[1] / "scripts" / "metrics_collect.py"
_spec = importlib.util.spec_from_file_location("metrics_collect", _script)
assert _spec is not None and _spec.loader is not None
mc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mc)

KST = timezone(timedelta(hours=9))
NOW = datetime.now().astimezone()
_seq = itertools.count(1)


def _ago(minutes: float) -> str:
    return (NOW - timedelta(minutes=minutes)).isoformat(timespec="seconds")


def _ev(eid, minutes_ago, kind, payload):
    """seq는 생성 순서(시간순)로 자동 부여 — collect가 seq 정렬하므로 시간순과 일치해야 한다."""
    return {"seq": next(_seq), "entity_id": eid, "ts": _ago(minutes_ago), "kind": kind,
            "payload": json.dumps(payload)}


def _issues(issues, events):
    def fake_get(base, path):
        if path.startswith("/events"):
            return {"events": events}
        return list(issues)
    return fake_get


def _run(events, issues, window_minutes_ago=180, state_file=None):
    mc.get = _issues(list(issues), events)
    ws = NOW - timedelta(minutes=window_minutes_ago)
    return mc.collect("http://test", str(state_file) if state_file else None, ws)


def _sub(eid, m):
    return _ev(eid, m, "issue.updated", {"state": "review", "fields": ["state", "completed_at"]})


def _done(eid, m):
    return _ev(eid, m, "issue.updated", {"state": "done", "fields": ["state", "completed_at"]})


def _claim(eid, m):
    return _ev(eid, m, "issue.updated", {"state": "in_progress", "fields": ["state", "started_at"]})


def _pull(eid, m):
    # /pull은 fields 없이 state만 운반한다(server/routers/issues.py:228-253 실측)
    return _ev(eid, m, "issue.updated", {"state": "in_progress", "assignee": "w", "via": "pull"})


def _ret(eid, m):
    return _ev(eid, m, "issue.updated", {"state": "todo", "fields": ["state"]})


def _meta(eid, m, state="todo"):
    return _ev(eid, m, "issue.updated", {"state": state, "fields": ["priority"]})


def _iss(eid, state, **kw):
    d = {"id": eid, "state": state}
    d.update(kw)
    return d


def test_same_second_submit_verify_pairing():
    """같은 초의 제출+검증 쌍도 상태값으로 페어링된다(라이브 7쌍 실측)."""
    m = _run([_ev("A", 60, "issue.created", {"state": "todo"}),
              _claim("A", 50), _sub("A", 40), _done("A", 40)],
             [_iss("A", "done", execution_attempt=1)])
    rp = m["review_pass"]
    assert (rp["submitted"], rp["passed"], rp["direct_done_no_submission"]) == (1, 1, 0)


def test_rework_cycle_preserves_instances():
    """제출→반납→재제출→검증: 제출 2/통과 1/반납 1 — v2의 마지막 제출만 보존 결함 회귀."""
    m = _run([_ev("A", 300, "issue.created", {"state": "todo"}),
              _claim("A", 290), _sub("A", 280), _ret("A", 270),
              _claim("A", 260), _sub("A", 250), _done("A", 240)],
             [_iss("A", "done", execution_attempt=2)], window_minutes_ago=400)
    rp = m["review_pass"]
    assert (rp["submitted"], rp["passed"], rp["reworked_after_submit"]) == (2, 1, 1)


def test_superseded_submission_closed():
    """review 상태에서 재제출하면 이전 회차는 superseded로 종료된다."""
    m = _run([_ev("A", 300, "issue.created", {"state": "todo"}),
              _claim("A", 290), _sub("A", 280), _sub("A", 270), _done("A", 260)],
             [_iss("A", "done", execution_attempt=1)], window_minutes_ago=400)
    rp = m["review_pass"]
    assert (rp["submitted"], rp["passed"], rp["superseded_submissions"]) == (2, 1, 1)


def test_pre_window_return_closes_submission_then_direct_done():
    """창 이전 제출→반납은 회차를 종료한다 — 창 내 force_done은 직행(1차 리뷰 R1 회귀)."""
    m = _run([_ev("A", 240, "issue.created", {"state": "todo"}),
              _claim("A", 230), _sub("A", 220), _ret("A", 210),
              _claim("A", 10), _done("A", 5)],
             [_iss("A", "done", execution_attempt=2)])
    rp = m["review_pass"]
    assert (rp["submitted"], rp["passed"], rp["reworked_after_submit"],
            rp["direct_done_no_submission"]) == (0, 0, 0, 1)


def test_direct_done_window_filter():
    """직행 카운트도 창 필터 — 창 이전 직행 1+창 내 1 → 1."""
    events = [_ev("A", 300, "issue.created", {"state": "todo"}),
              _ev("A", 290, "issue.updated", {"state": "done", "fields": ["state", "completed_at"]}),
              _ev("A", 10, "issue.updated", {"state": "done", "fields": ["state", "completed_at"]})]
    m = _run(events, [_iss("A", "done")])
    assert m["review_pass"]["direct_done_no_submission"] == 1


def test_recovery_rate_120m_denominator(tmp_path):
    """120분 복구율 분모는 시간 유효 전체 해소분 — 30분 복구+180분 반납 → 50%(3차 리뷰 R2)."""
    sf = tmp_path / "state.json"
    sf.write_text(json.dumps({"open": {"A": {"id": "A", "first_seen": _ago(30)},
                                       "B": {"id": "B", "first_seen": _ago(180)}},
                               "resolved": []}))
    issues = [_iss("A", "in_progress", heartbeat_at=_ago(0), lease_expires=_ago(-60)),
              _iss("B", "todo")]
    m = _run([], issues, state_file=sf)
    s = m["stall"]
    assert s["recovery_rate_120m_pct"] == 50.0 and s["timed_resolved_n"] == 2
    outcomes = {r["id"]: r["outcome"] for r in s["resolved_rows"]}
    assert outcomes == {"A": "recovered", "B": "returned"}


def test_return_count_window_filter():
    """이전 주 반납은 세지 않고 창 내 반납만 누적(3차 리뷰 R3)."""
    events = [_ev("A", 600, "issue.created", {"state": "todo"}),
              _claim("A", 590), _sub("A", 580), _ret("A", 570),
              _claim("A", 10), _sub("A", 5), _ret("A", 2)]
    m = _run(events, [_iss("A", "todo")])
    assert m["rework"]["review_to_todo_returns"] == 1


def test_metadata_edit_does_not_overwrite_todo_entry():
    """todo 카드 메타데이터 수정은 진입 시각을 덮어쓰지 않는다(3차 리뷰 R4)."""
    events = [_ev("A", 30, "issue.created", {"state": "todo"}),
              _meta("A", 20), _pull("A", 0)]
    m = _run(events, [_iss("A", "in_progress", started_at=_ago(0))])
    rows = m["time_to_start"]["cohort_rows"]
    assert len(rows) == 1 and rows[0]["minutes"] == 30.0


def test_pull_receipt_in_cohort():
    """/pull 수령도 첫 수령으로 인정된다(2차 리뷰 R4)."""
    events = [_ev("A", 30, "issue.created", {"state": "todo"}), _pull("A", 0)]
    m = _run(events, [_iss("A", "in_progress", started_at=_ago(0))])
    assert m["time_to_start"]["cohort_n"] == 1


def test_intervention_window_filter():
    """개입 카운터(사람 코멘트·blocked 진입)는 창 내 이벤트만 센다(5차 리뷰 Intervention_window)."""
    events = [_ev("A", 600, "issue.created", {"state": "todo"}),
              _claim("A", 590),
              _ev("A", 500, "comment.added", {"author": "skshim"}),            # 창 이전
              _ev("A", 10, "comment.added", {"author": "skshim"}),             # 창 내
              _ev("B", 600, "issue.created", {"state": "todo"}),
              _ev("B", 500, "issue.updated", {"state": "blocked", "fields": ["state"]}),  # 창 이전 진입
              _ret("B", 490),                                                  # 해제 → 직전 상태 복원
              _ev("B", 5, "issue.updated", {"state": "blocked", "fields": ["state"]}),     # 창 내 진입
              _ev("B", 3, "issue.updated", {"state": "blocked", "fields": ["priority"]})]  # 메타데이터 수정
    m = _run(events, [_iss("A", "in_progress"), _iss("B", "blocked")])
    iv = m["intervention"]
    assert iv["human_comments"] == 1 and iv["blocked_entries"] == 1


def test_python39_timestamp_formats():
    """서버 타임스탬프 형식(Z·콜론 없는 ±HHMM·분수 초)이 3.9에서도 파싱된다."""
    for s in ("2026-10-09T13:38:33Z", "2026-10-09T13:38:33+0900",
              "2026-10-09T13:38:33.123456+0900", "2026-10-09T13:38:33-0530"):
        assert mc.parse_ts(s) is not None
