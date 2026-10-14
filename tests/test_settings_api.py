"""리뷰어 라우팅 런타임 설정 API — 웹 편집 가능한 순서·맵(XV84).

계약: 코드 상수(DEFAULT_REVIEW_MAP·REVIEW_FALLBACK)는 부팅 기본값. 저장 설정이
있으면 probe가 사이클마다 읽어 덮는다 — 서버 재배포 없이 순서 조정."""
import pytest
from fastapi.testclient import TestClient

import app as appmod


@pytest.fixture()
def client(tmp_path):
    return TestClient(appmod.create_app(str(tmp_path / "tt.db")))


def test_미저장시_기본값_반환(client):
    d = client.get("/settings/review-routing").json()
    assert d["source"] == "default"
    assert isinstance(d["review_fallback"], list) and d["review_fallback"]
    assert all(isinstance(x, str) for x in d["review_fallback"])
    assert "agy" in d["review_fallback"] and "cursor-review" in d["review_fallback"]
    assert isinstance(d["review_map"], dict) and "codex" in d["review_map"]


def test_저장후_조회(client):
    r = client.put("/settings/review-routing", json={
        "review_fallback": ["cursor-review", "agy", "codex", "claude"],
        "review_map": {"codex": "claude", "claude": "codex"}})
    assert r.status_code == 200
    d = client.get("/settings/review-routing").json()
    assert d["source"] == "stored"
    assert d["review_fallback"] == ["cursor-review", "agy", "codex", "claude"]
    assert d["review_map"]["codex"] == "claude"
    assert d.get("updated_at")


def test_불량_설정은_422(client):
    assert client.put("/settings/review-routing",
                      json={"review_fallback": []}).status_code == 422
    assert client.put("/settings/review-routing",
                      json={"review_fallback": ["agy", 1]}).status_code == 422
    assert client.put("/settings/review-routing",
                      json={"review_fallback": ["x"] * 13}).status_code == 422
    assert client.put("/settings/review-routing",
                      json={"review_fallback": ["agy"],
                            "review_map": {"codex": 3}}).status_code == 422
    # 삭제 후 기본값 복귀
    client.put("/settings/review-routing", json={"review_fallback": ["agy"]})
    assert client.delete("/settings/review-routing").status_code == 200
    assert client.get("/settings/review-routing").json()["source"] == "default"


def test_설정_키는_review_routing만(client):
    assert client.put("/settings/other",
                      json={"review_fallback": ["agy"]}).status_code == 404
