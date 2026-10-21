"""M5BNMHB7-RBQE — 예산 축 claim 횟수 전환.

execution_attempt는 회차 ID(본문 수정·stale 보고 거부)로 남고, 자동 배정 예산은
claim+pull 수령 횟수(claim_count)로 판정한다. 본문 수정만으로 오른 attempt는
예산을 소모하지 않는다.
"""
import json
import sqlite3

import db as dbmod
from conftest import mk


def _legacy_db(path, issues, events):
    """claim_count 이전 스키마 — SCHEMA에는 컬럼이 없으므로 그대로 쓰면 레거시다."""
    con = sqlite3.connect(path)
    con.executescript(dbmod.SCHEMA)
    for i in issues:
        con.execute("INSERT INTO issues (id, title, state, created_at, updated_at, execution_attempt) "
                    "VALUES (?,?,?,?,?,?)",
                    (i["id"], i["id"], "todo", i["created_at"], i["created_at"], i["attempt"]))
    for kind, eid, payload, ts in events:
        con.execute("INSERT INTO events (kind, entity, entity_id, payload, ts, notified) "
                    "VALUES (?,?,?,?,?,1)", (kind, "issue", eid, json.dumps(payload, ensure_ascii=False), ts))
    con.commit()
    con.close()


def test_backfill_counts_claim_events_in_events_era(tmp_path):
    p = str(tmp_path / "tt.db")
    _legacy_db(p, [
        {"id": "A", "created_at": "2026-10-12T09:00:00+0900", "attempt": 0},
        {"id": "B", "created_at": "2026-10-12T09:00:00+0900", "attempt": 0},
        {"id": "C", "created_at": "2026-10-01T09:00:00+0900", "attempt": 4},
    ], [
        # A: pull 2회 + claim 1회
        ("issue.updated", "A", {"state": "in_progress", "assignee": "w", "via": "pull"},
         "2026-10-10T10:00:00+0900"),
        ("issue.updated", "A", {"state": "in_progress", "assignee": "w", "via": "pull"},
         "2026-10-10T11:00:00+0900"),
        ("issue.updated", "A", {"state": "in_progress",
                                "fields": ["assignee", "lease_by", "started_at", "state"]},
         "2026-10-10T12:00:00+0900"),
        # B: 본문 수정만 — 예산 아님
        ("issue.updated", "B", {"state": "todo",
                                "fields": ["body", "execution_attempt", "state", "work_contract"]},
         "2026-10-10T10:30:00+0900"),
    ])
    con = dbmod.connect(p)
    got = {r["id"]: r["claim_count"] for r in con.execute("SELECT id, claim_count FROM issues")}
    con.close()
    assert got == {"A": 3, "B": 0, "C": 4}  # C는 events 이전 — attempt 보수적 유지


def test_backfill_runs_once_only(tmp_path):
    """재기동마다 덮지 않는다 — 컬럼 존재 시 백필 스킵(수령 후 값 보존)."""
    p = str(tmp_path / "tt.db")
    _legacy_db(p, [{"id": "A", "created_at": "2026-10-10T09:00:00+0900", "attempt": 0}], [])
    con = dbmod.connect(p)
    con.execute("UPDATE issues SET claim_count=1 WHERE id='A'")
    con.commit()
    con.close()
    con = dbmod.connect(p)  # 재연결(재기동) — 백필 미실행
    assert con.execute("SELECT claim_count FROM issues WHERE id='A'").fetchone()["claim_count"] == 1
    con.close()


def test_patch_body_edits_do_not_consume_budget(client):
    """재현 시나리오 — 본문 수정 2회(attempt 2, claim 0) 후에도 auto pull이 배정한다."""
    i = mk(client, labels=["auto"], brief_verify="pytest -q", title="v1")
    for n in range(2):
        cur = client.get(f"/issues/{i['id']}").json()
        r = client.patch(f"/issues/{i['id']}", json={"body": f"본문 수정 {n}",
                                                     "expected_version": cur["version"]})
        assert r.status_code == 200
    cur = client.get(f"/issues/{i['id']}").json()
    assert cur["execution_attempt"] == 2  # 회차 ID는 오른다
    assert cur["claim_count"] == 0        # 예산은 소모되지 않는다
    got = client.post("/pull", json={"agent": "cron", "require_label": "auto"}).json()
    assert got["id"] == i["id"] and got["claim_count"] == 1


def test_budget_exhausts_after_two_claim_counts(client):
    """claim 2회(본문 수정 무관) → 예산 소진 — 수동 claim은 todo에서 여전히 자유."""
    i = mk(client)
    for n in range(2):
        got = client.post(f"/issues/{i['id']}/claim", json={"agent": f"w{n}"}).json()
        assert got["claim_count"] == n + 1
        client.patch(f"/issues/{i['id']}", json={"state": "todo"})
    assert client.post(f"/issues/{i['id']}/claim", json={"agent": "w3"}).status_code == 200
