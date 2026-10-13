"""tt-wake-relay 단위 테스트 (M4T3TYD4-N5Q9).

SSE 파싱·필터 로직·프롬프트 빌드·TUI 주입 POST를 검증한다.
실행: python3 -m pytest tests/test_wake_relay.py -q (repo root)
"""

import importlib.util as _ilu
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = _ilu.spec_from_file_location(
    "tt_wake_relay", os.path.join(_HERE, "..", "scripts", "tt-wake-relay.py"))
R = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(R)


def _row(kind="comment.added", entity="issue", entity_id="I-1",
         payload=None, seq=100):
    return {"seq": seq, "kind": kind, "entity": entity, "entity_id": entity_id,
            "payload": json.dumps(payload or {}, ensure_ascii=False),
            "ts": "2026-10-13T00:00:00", "notified": 1}


def _cache(assignee="skshim@tp-13"):
    """항상 같은 이슈를 돌려주는 고정 캐시."""
    class Fixed:
        def __init__(self, a):
            self.a = a

        def get(self, issue_id):
            return {"id": issue_id, "assignee": self.a}
    return Fixed(assignee)


# ---------- SSE 파싱 ----------

def test_parse_sse_basic_event():
    lines = ["id: 41", "event: comment.added",
             'data: {"seq": 41, "kind": "comment.added", "entity_id": "X-1"}', ""]
    out = list(R.parse_sse(lines))
    assert out == [(41, "comment.added",
                    {"seq": 41, "kind": "comment.added", "entity_id": "X-1"})]


def test_parse_sse_skips_ping_and_incomplete():
    lines = [": ping", "", "event: issue.updated", ""]  # data 없는 블록 → 무시
    assert list(R.parse_sse(lines)) == []


def test_parse_sse_accepts_bytes_and_ping_among_events():
    lines = [b": ping", b"id: 7", b"event: dispatch.updated",
             b'data: {"entity": "dispatch"}', b""]
    out = list(R.parse_sse(lines))
    assert out == [(7, "dispatch.updated", {"entity": "dispatch"})]


# ---------- 필터 로직 ----------

def test_comment_signal_on_my_card_wakes():
    row = _row(payload={"author": "agy", "preview": "review: approve\n좋다"})
    prompt = R.handle_event(row, cache=_cache())
    assert prompt is not None and "I-1" in prompt and "review: approve" in prompt


def test_comment_merge_hash_wakes():
    row = _row(payload={"author": "claude", "preview": "PR delivery: ... merge=fb9c5675"})
    assert R.handle_event(row, cache=_cache()) is not None


def test_self_comment_never_wakes():
    row = _row(payload={"author": "skshim@tp-13",
                        "preview": "merge=abc 내 보고"})
    assert R.handle_event(row, cache=_cache()) is None


def test_other_assignee_card_ignores():
    row = _row(payload={"author": "agy", "preview": "review: approve"})
    assert R.handle_event(row, cache=_cache(assignee="codex@m2max")) is None


def test_dispatch_entity_ignores_without_issue_fetch():
    row = _row(kind="dispatch.updated", entity="dispatch", entity_id="D-9",
               payload={"state": "done"})
    assert R.handle_event(row, cache=None) is None


def test_issue_updated_done_and_blocked_wake():
    done = _row(kind="issue.updated", payload={"state": "done", "fields": ["state"]})
    assert R.handle_event(done, cache=_cache()) is not None
    blocked = _row(kind="issue.updated", payload={"state": "blocked"})
    assert R.handle_event(blocked, cache=_cache()) is not None


def test_issue_updated_in_progress_no_wake():
    row = _row(kind="issue.updated", payload={"state": "in_progress"})
    assert R.handle_event(row, cache=_cache()) is None


def test_comment_without_signal_no_wake():
    row = _row(payload={"author": "agy", "preview": "진행 중입니다"})
    assert R.handle_event(row, cache=_cache()) is None


# ---------- 프롬프트 빌드 ----------

def test_signal_prompt_contains_issue_and_reason():
    p = R.signal_prompt("M4X-1", "카드 상태 → done", '["state"]')
    assert "M4X-1" in p and "done" in p and "TT 보드를 확인" in p


# ---------- 주입 POST ----------

class _Handler(BaseHTTPRequestHandler):
    posted = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        _Handler.posted.append((self.path, json.loads(body)))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"true")

    def log_message(self, *a):
        pass


@pytest.fixture()
def fake_tui():
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    _Handler.posted = []
    yield "http://127.0.0.1:%d" % srv.server_address[1]
    srv.shutdown()


def test_inject_posts_append_then_submit(fake_tui):
    assert R.inject(fake_tui, text="웨이크 텍스트") is True
    paths = [p for p, _ in _Handler.posted]
    assert paths == ["/tui/append-prompt", "/tui/submit-prompt"]
    assert _Handler.posted[0][1] == {"text": "웨이크 텍스트"}


def test_inject_false_when_tui_down():
    # 아무것도 안 떠 있는 포트 → 조용히 False (예외가 밖으로 새면 안 된다)
    assert R.inject("http://127.0.0.1:1", text="x") is False


# ---------- 커서 ----------

def test_cursor_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(R, "CURSOR_PATH", str(tmp_path / "seq"))
    assert R.read_cursor() == 0
    R.write_cursor(3239)
    assert R.read_cursor() == 3239
