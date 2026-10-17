"""X7DG(M5283NV9-X7DG) — 리뷰어 보정: 리뷰어별 approve/RC 집계와 승인 후 발견 결함.

판정 코멘트는 `review: approve|request-changes` 본문으로 시작하고 작성자가 리뷰어다.
같은 카드에서 승인( seq s1 ) 뒤에 RC( seq s2 > s1 )가 나오면 승인자에게 결함 1건
(카드당 리뷰어당 1회). RC→fix→재승인→재RC 루프에서도 승인 시점마다 재판정한다.
"""
import importlib.util
import itertools
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_script = Path(__file__).resolve().parents[1] / "scripts" / "metrics_collect.py"
_spec = importlib.util.spec_from_file_location("metrics_collect", _script)
mc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mc)

KST = timezone(timedelta(hours=9))
NOW = datetime.now().astimezone()
_seq = itertools.count(1)


def _ago(minutes: float) -> str:
    return (NOW - timedelta(minutes=minutes)).isoformat(timespec="seconds")


def _verdict(eid, minutes_ago, author, verdict):
    return {"seq": next(_seq), "entity_id": eid, "ts": _ago(minutes_ago), "kind": "comment.added",
            "payload": json.dumps({"author": author, "preview": f"review: {verdict}\nTT#{eid}@a1/{'x'*64}"})}


def _issue_updated(eid, minutes_ago, state):
    return {"seq": next(_seq), "entity_id": eid, "ts": _ago(minutes_ago), "kind": "issue.updated",
            "payload": json.dumps({"state": state, "fields": ["state"]})}


def _collect(events, issues=()):
    mc.get = lambda base, path: (list(issues) if path.startswith("/issues") else None)
    mc.fetch_events = lambda base, max_seq=None: sorted(events, key=lambda e: e["seq"])
    ws = NOW - timedelta(minutes=600)
    return mc.collect("http://test", None, ws)


def _row(m, reviewer):
    return next(r for r in m["reviewers"]["rows"] if r["reviewer"] == reviewer)


def test_verdict_counts_per_reviewer():
    events = [_verdict("C1", 60, "agy", "approve"),
              _verdict("C2", 50, "agy", "approve"),
              _verdict("C3", 40, "codex", "request-changes"),
              _verdict("C4", 30, "codex", "approve")]
    m = _collect(events)
    assert _row(m, "agy")["approve"] == 2
    assert _row(m, "agy")["request_changes"] == 0
    assert _row(m, "codex")["request_changes"] == 1
    assert _row(m, "codex")["approve"] == 1
    assert m["reviewers"]["post_approval_defects_total"] == 0


def test_post_approval_defect_attributed_to_approver():
    events = [_verdict("C1", 60, "agy", "approve"),
              _verdict("C1", 30, "codex", "request-changes")]
    m = _collect(events)
    assert _row(m, "agy")["post_approval_defects"] == 1
    assert _row(m, "codex")["post_approval_defects"] == 0
    assert m["reviewers"]["post_approval_defects_total"] == 1


def test_no_defect_when_rc_precedes_approval():
    events = [_verdict("C1", 60, "codex", "request-changes"),
              _verdict("C1", 30, "agy", "approve")]
    m = _collect(events)
    assert m["reviewers"]["post_approval_defects_total"] == 0


def test_defect_counted_once_per_approver_card():
    events = [_verdict("C1", 60, "agy", "approve"),
              _verdict("C1", 50, "agy", "approve"),
              _verdict("C1", 30, "codex", "request-changes")]
    m = _collect(events)
    assert _row(m, "agy")["post_approval_defects"] == 1


def test_reapproval_resets_then_new_rc_counts_again():
    # 승인 → RC(결함 1) → 재승인 → 재RC(결함 2) — 승인 시점별 판정
    events = [_verdict("C1", 60, "agy", "approve"),
              _verdict("C1", 50, "codex", "request-changes"),
              _verdict("C1", 40, "agy", "approve"),
              _verdict("C1", 30, "codex", "request-changes")]
    m = _collect(events)
    assert _row(m, "agy")["post_approval_defects"] == 2


def test_non_verdict_comments_ignored():
    events = [{"seq": next(_seq), "entity_id": "C1", "ts": _ago(60), "kind": "comment.added",
               "payload": json.dumps({"author": "runner@m2max",
                                      "preview": "runner:m2max dispatch#1 done exit=0\nreview: request-changes\n"})},
              {"seq": next(_seq), "entity_id": "C1", "ts": _ago(50), "kind": "comment.added",
               "payload": json.dumps({"author": "probe", "preview": "[tt-review C1] reviewer=agy"})},
              {"seq": next(_seq), "entity_id": "C1", "ts": _ago(40), "kind": "issue.updated",
               "payload": json.dumps({"state": "review", "fields": ["state"]})}]
    m = _collect(events)
    assert m["reviewers"]["rows"] == []
    assert m["reviewers"]["post_approval_defects_total"] == 0


def test_window_excludes_pre_window_verdicts():
    events = [_verdict("C1", 700, "agy", "approve"),   # 창 밖
              _verdict("C2", 60, "agy", "approve"),
              _verdict("C2", 30, "codex", "request-changes")]
    m = _collect(events)
    assert _row(m, "agy")["approve"] == 1
    assert _row(m, "agy")["post_approval_defects"] == 1  # 창 밖 승인은 결함 근거 제외


def test_reviewers_in_full_collect_output(tmp_path):
    issues = [{"id": "C1", "state": "review"}]
    events = [_verdict("C1", 60, "agy", "request-changes")]
    m = _collect(events, issues)
    assert m["reviewers"]["rows"][0]["total"] == 1
    assert m["reviewers"]["rows"][0]["approve_pct"] == 0.0


def test_word_boundary_rejects_lookalikes():
    """agy RC — 'review: approved-by-x' 류 비정규 문구 오인 방지(첫 줄 완전 일치)."""
    events = [{"seq": next(_seq), "entity_id": "C1", "ts": _ago(60), "kind": "comment.added",
               "payload": json.dumps({"author": "agy", "preview": "review: approved-by-x\nTT#C1@a1/x"})},
              {"seq": next(_seq), "entity_id": "C2", "ts": _ago(50), "kind": "comment.added",
               "payload": json.dumps({"author": "agy", "preview": "review: request-changes-pending"})},
              _verdict("C3", 40, "agy", "approve")]
    m = _collect(events)
    assert [_r["reviewer"] for _r in m["reviewers"]["rows"]] == ["agy"]
    assert m["reviewers"]["rows"][0]["approve"] == 1


def test_defect_rows_kept_by_recency_not_alphabet():
    """agy RC — 결함 20건 초과 시 알파벳순이 아닌 rc_seq 최신순으로 슬라이스."""
    events = []
    for i in range(25):
        approver = "aa" if i % 2 == 0 else "zz"
        events.append(_verdict(f"C{i}", 600 - i * 2, approver, "approve"))
        events.append(_verdict(f"C{i}", 599 - i * 2, "rr", "request-changes"))
    m = _collect(events)
    rows = m["reviewers"]["defect_rows"]
    assert len(rows) == 20
    seqs = [r["rc_seq"] for r in rows]
    assert seqs == sorted(seqs)                      # rc_seq 오름차순 — 최신 20건 보존
    assert {r["reviewer"] for r in rows} == {"aa", "zz"}  # 알파벳 앞글자(aa) 리뷰어도 보존
