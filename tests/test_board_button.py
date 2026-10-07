"""에이전트 보드 시각화 (M4A55K92-4BZK): 메인/모바일 화면 상단 버튼 연결 + 안읽음 배지.

- GET / 와 /m 에 '에이전트 보드' 링크 + ab-badge 배지 요소 + tt-board-badge.js 로딩
- 배지 스크립트는 자기완결: 보드와 같은 시점 키(ab-viewer), /messages/unread 폴링(5분),
  tt-util 등 공용 스크립트 의존 없음
- 보드 본문 렌더 보강: 코드펜스 <pre>, 카드 ID → /#<id> 상세 링크, URL 앵커
"""
import sys

import pytest
from fastapi.testclient import TestClient

from app import create_app


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "p.db"))
    with TestClient(app) as c:
        yield c


def test_메인_화면에_보드_버튼(client):
    html = client.get("/").text
    assert 'href="/agent-board"' in html, "상단바에 보드 링크"
    assert 'id="ab-badge"' in html, "안읽음 배지 요소"
    assert "tt-board-badge.js" in html, "배지 스크립트 로딩"


def test_모바일에_보드_버튼(client):
    html = client.get("/m").text
    assert 'href="/agent-board"' in html, "모바일 헤더에 보드 링크"
    assert 'id="ab-badge"' in html
    assert "tt-board-badge.js" in html


def test_배지_스크립트_자기완결(client):
    js = client.get("/js/tt-board-badge.js").text
    assert '"ab-viewer"' in js, "보드 페이지와 같은 시점 키 — localStorage"
    assert "/messages/unread" in js, "안읽음 API 폴링"
    assert "300000" in js, "5분 주기 폴링"
    assert "tt-util" not in js and "AB.api" not in js, "공용 스크립트 의존 금지"


def test_보드_본문_렌더_보강(client):
    js = client.get("/js/tt-agent-board.js").text
    assert "<pre>" in js, "코드펜스 렌더"
    assert "location.hash" in js or 'href="/#' in js, "카드 ID → 상세 링크"
    assert "https?" in js or "http" in js, "URL 앵커"
