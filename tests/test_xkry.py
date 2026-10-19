"""XKRY(M58SPRZ7-XKRY) — 엔티티 정본 카탈로그(card·PR·agent·reviewer) + 생성 게이트.

정본 해석 단일 경로 + 미등록 문자열의 unknown_entity 기록(원문 id만, 본문 저장 금지).
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

import pytest

from ontology import catalog


AGENTS = [
    {"name": "hermes", "aliases": ["kanban-adapter"]},
    {"name": "opencode-tp13", "aliases": []},
    {"name": "cursor-review", "aliases": []},
]


@pytest.fixture(autouse=True)
def _reset_unknown():
    catalog._unknown_counts.clear()
    catalog._unknown_log.clear()
    yield
    catalog._unknown_counts.clear()
    catalog._unknown_log.clear()


# ---------- agent: identity 단일 소스 위임 ----------

def test_agent_exact_and_alias():
    assert catalog.resolve("agent", "hermes", agents=AGENTS) == "hermes"
    assert catalog.resolve("agent", "kanban-adapter", agents=AGENTS) == "hermes"


def test_agent_unregistered_records_unknown_id_only():
    assert catalog.resolve("agent", "ghost-agent", agents=AGENTS) is None
    rec = catalog.unknown_log()[-1]
    assert set(rec) == {"kind", "raw_id", "n"}  # id만 — 본문 필드 없음(TV Q4)
    assert rec["kind"] == "agent" and rec["raw_id"] == "ghost-agent"
    assert catalog.unknown_counts()["agent"] == 1


def test_agent_case_and_suffix_variants():
    # identity 계약(정확명 대소문자 구분) 그대로 — 카탈로그가 정규화로 계약을 바꾸지 않는다
    assert catalog.resolve("agent", "Hermes", agents=AGENTS) is None
    assert catalog.resolve("agent", "hermes@mini", agents=AGENTS) is None
    assert catalog.unknown_counts()["agent"] == 2
    # 가족 폴백은 옵션(probe 스냅샷 소비 전용) — 켜면 접미 변형도 정본화
    assert catalog.resolve("agent", "hermes@mini", agents=AGENTS,
                           family_fallback=True) == "hermes"


def test_reviewer_strict_no_family_fallback():
    assert catalog.resolve("reviewer", "cursor-review", agents=AGENTS) == "cursor-review"
    assert catalog.resolve("reviewer", "cursor", agents=AGENTS) is None  # 가족 폴백 없음


# ---------- card ----------

def test_card_canonical():
    assert catalog.resolve("card", "M58SPRZ7-XKRY") == "M58SPRZ7-XKRY"
    assert catalog.resolve("card", "  m58sprz7-xkry ") == "M58SPRZ7-XKRY"


def test_card_invalid_is_unknown_not_promoted():
    assert catalog.resolve("card", "not-a-card-id!!") is None
    assert catalog.resolve("card", "") is None
    assert catalog.unknown_counts()["card"] == 2


# ---------- pr ----------

def test_pr_ref_and_url_canonical():
    assert catalog.resolve("pr", "plainOldCode/think-tank#171") == "plainOldCode/think-tank#171"
    assert catalog.resolve("pr", "https://github.com/plainOldCode/think-tank/pull/171") \
        == "plainOldCode/think-tank#171"


def test_pr_repo_case_preserved():
    # 조인이 정확 문자열 매치 — 카탈로그가 케이스를 바꾸면 계약이 깨진다
    assert catalog.resolve("pr", "PlainOldCode/Think-Tank#9") == "PlainOldCode/Think-Tank#9"


def test_pr_invalid_is_unknown():
    assert catalog.resolve("pr", "think-tank#9") is None  # owner 없음
    assert catalog.resolve("pr", "o/r/p#9") is None
    assert catalog.resolve("pr", "plain text") is None
    assert catalog.unknown_counts()["pr"] == 3


# ---------- kind 가드 ----------

def test_unknown_kind_raises():
    with pytest.raises(ValueError):
        catalog.resolve("planet", "earth")


# ---------- dispatch 대상 검증 통일(기존 응답 계약 유지) ----------

def test_dispatch_unregistered_target_404_via_catalog(client):
    r = client.post("/issues", json={"title": "t", "acceptance": "c"})
    iid = r.json()["id"]
    resp = client.post(f"/issues/{iid}/dispatch", json={"agent": "ghost", "message": "x"})
    assert resp.status_code == 404  # 기존 계약 유지(test_api.py:749 회귀)


def test_dispatch_alias_target_resolves_via_catalog(client):
    assert client.post("/agents", json={"name": "alias-src", "aliases": ["alias-registered"],
                                        "base_url": "http://127.0.0.1:1/hook",
                                        "receive_mode": "hook"}).status_code == 201
    iid = client.post("/issues", json={"title": "t", "acceptance": "c"}).json()["id"]
    resp = client.post(f"/issues/{iid}/dispatch", json={"agent": "alias-registered", "message": "x"})
    assert resp.status_code == 201, resp.text  # 별칭 → 카탈로그 경로로 정본화


def test_unknown_entity_log_never_holds_bodies():
    catalog.resolve("card", "M4ABCDEF-GH12 some very long prose body that is not an id")
    for rec in catalog.unknown_log():
        assert "body" not in rec and "raw" not in rec
        assert len(rec["raw_id"]) <= 120
