"""RPXH(M53PBDWJ-RPXH) — 369Y 후속: 반복 지적의 카드 간 집계·전환 연결,
premise 재검토 상태 강제, 계약 문서 동기화의 테스트 전환.

369Y는 카드 하나 안에서만 반복을 셌다. RPXH는
1) GET /rejections/summary — 기간 내 반려 유형을 카드 간 집계
2) 재작업 발령문에 전환 권장 노트(반복 유형 → lint/테스트/계약 체크)
3) 반려 3회+ 카드는 premise_rework 상태(컬럼·표시·verify/claim 시 소거)
"""
import json

import probe.core as core


# --- 1) 카드 간 집계 ---

def test_반려유형은_카드_전체에서_집계된다(client):
    for title in ("a", "b", "c"):
        iid = client.post("/issues", json={"title": title, "type": "task",
                                           "acceptance": "완료 기준: 테스트 통과"}).json()["id"]
        for t in (["계약 문서 패리티 누락", "api.md 동기화 빠짐"] if title != "c"
                  else ["계약 문서 패리티 누락"]):
            client.post(f"/issues/{iid}/comments",
                        json={"author": "agy", "body": f"review: request-changes\nTT#{iid}@a1/x\n{t}"})
    r = client.get("/rejections/summary?days=30")
    assert r.status_code == 200, r.text
    d = r.json()
    buckets = {b["bucket"]: b for b in d["buckets"]}
    assert buckets["contract"]["count"] == 5 and buckets["contract"]["cards"] == 3


def test_집계는_기간_밖을_세지_않는다(client, tmp_path):
    import sqlite3
    import time

    iid = client.post("/issues", json={"title": "오래된 카드", "type": "task",
                                       "acceptance": "완료 기준: 테스트 통과"}).json()["id"]
    client.post(f"/issues/{iid}/comments",
                json={"author": "agy", "body": "review: request-changes\nTT#x@a1/y\n테스트 없음"})
    old = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(time.time() - 90 * 86400))
    con = sqlite3.connect(tmp_path / "tt.db")
    con.execute("UPDATE comments SET ts=?", (old,))
    con.commit()
    con.close()
    r = client.get("/rejections/summary?days=30")
    assert r.status_code == 200 and r.json()["buckets"] == [], r.text


# --- 2) 전환 노트(순수) ---

def test_전환_노트는_카드간_반복_유형을_권장한다():
    summary = {"days": 30, "buckets": [
        {"bucket": "contract", "count": 5, "cards": 3, "last_ts": "2026-10-17"},
        {"bucket": "test", "count": 1, "cards": 1, "last_ts": "2026-10-17"},
        {"bucket": "style", "count": 2, "cards": 2, "last_ts": "2026-10-17"},
    ]}
    cur = {"comments": [{"author": "agy", "ts": "t",
                         "body": "review: request-changes\nTT#x@a1/z\n계약 문서 패리티 누락"}]}
    s = core._conversion_section(summary, cur)
    assert "contract" in s and "전환" in s, s
    assert "test" not in s  # 반복 아님 — 제외
    assert "style" not in s  # 카드 2장이지만 3회 미만 — 제외


def test_전환_노트는_신호_없으면_빈값():
    summary = {"days": 30, "buckets": []}
    assert core._conversion_section(summary, {}) == ""


# --- 3) premise_rework 상태 ---

def test_premise_rework_설정과_표시(client):
    iid = client.post("/issues", json={"title": "3회 반려 카드", "type": "task",
                                       "acceptance": "완료 기준: 테스트 통과"}).json()["id"]
    r = client.patch(f"/issues/{iid}", json={"premise_rework": True})
    assert r.status_code == 200, r.text
    assert client.get(f"/issues/{iid}").json()["premise_rework"] == 1


def test_premise_rework는_검증_종결에_소거된다(client):
    iid = client.post("/issues", json={"title": "재발 카드", "type": "task",
                                       "acceptance": "완료 기준: 테스트 통과"}).json()["id"]
    client.patch(f"/issues/{iid}", json={"premise_rework": True})
    issue = client.post(f"/issues/{iid}/claim", json={"agent": "worker", "hours": 2}).json()
    rep = {"contract_version": issue["work_contract"]["version"],
           "attempt": issue["execution_attempt"], "method": "planned", "result": "passed",
           "design": {"criteria": "c", "verification": "v"},
           "implementation": {"summary": "s", "commands": "cc"},
           "verification": {"commands": "ccc", "evidence": "1 passed"}}
    r = client.patch(f"/issues/{iid}", json={"state": "done", "completion_report": rep})
    assert r.status_code == 200, r.text
    r = client.post(f"/issues/{iid}/verify", json={"verifier": "tester", "human": True,
                                                   "evidence": "승인"})
    assert r.status_code == 200, r.text
    assert client.get(f"/issues/{iid}").json()["premise_rework"] == 0


def test_premise_rework는_새_수령에서_초기화된다(client):
    iid = client.post("/issues", json={"title": "재수령 카드", "type": "task",
                                       "acceptance": "완료 기준: 테스트 통과"}).json()["id"]
    client.patch(f"/issues/{iid}", json={"premise_rework": True})
    r = client.post(f"/issues/{iid}/claim", json={"agent": "worker", "hours": 2})
    assert r.status_code == 200, r.text
    assert client.get(f"/issues/{iid}").json()["premise_rework"] == 0
