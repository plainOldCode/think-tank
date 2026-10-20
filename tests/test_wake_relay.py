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

# 모듈이 임포트 시 env를 읽으므로 기본값을 먼저 고정한다 (기기별 env 영향 제거).
os.environ.setdefault("TT_URL", "http://tt.test:1")
os.environ.setdefault("TT_AGENT", "tester@ci")

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


def _cache(assignee=None):
    """항상 같은 이슈를 돌려주는 고정 캐시."""
    assignee = assignee or R.TT_AGENT

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

@pytest.mark.parametrize("kind,payload,expect", [
    ("comment.added", {"author": "agy", "preview": "review: approve\n좋다"},
     ["I-1", "review: approve"]),
    ("comment.added", {"author": "reviewer@host",
                       "preview": "review: request-changes\n필수 수정 1) ..."}, None),
    ("comment.added", {"author": "claude",
                       "preview": "PR delivery: github:x#113 head=aa base=main\nmerge=fb9c5675"}, None),
    # 7FQH — probe ci-fix 위임 코멘트(작업자 wake). preview[:120] 잘림 대비
    # [CI:fail]은 첫 줄, 마커 dedup 라인은 둘째 줄.
    ("comment.added", {"author": "probe",
                       "preview": "[CI:fail] @tester@ci #115 — tests 확인하라\n"
                                  "[ci-fix a3 #115/7941d573] PR#115 CI 실패 — 위임"},
     ["[CI:fail]", "#115"]),
    ("issue.updated", {"state": "done", "fields": ["state"]}, None),
    ("issue.updated", {"state": "blocked", "fields": ["state"]}, None),
], ids=["comment_signal_on_my_card", "comment_request_changes", "comment_merge_hash",
        "probe_ci_fail_comment_wakes_assignee", "issue_updated_done", "issue_updated_blocked"])
def test_wake_signals(kind, payload, expect):
    row = _row(kind=kind, payload=payload)
    prompt = R.handle_event(row, cache=_cache())
    assert prompt is not None
    for substr in expect or []:
        assert substr in prompt


def test_probe_ci_fail_다른_담당자면_무시():
    row = _row(payload={"author": "probe",
                        "preview": "[CI:fail] @tester@ci #115 — tests 확인하라"})
    assert R.handle_event(row, cache=_cache(assignee="other@host")) is None


@pytest.mark.parametrize("kind,payload,cache_assignee", [
    # 담당자 불일치 캐시 — 다른 카드 이벤트는 깨우지 않는다
    ("comment.added", {"author": "agy", "preview": "review: approve"}, "other@host"),
    # 판정 필수 1 — 접수 안내 문구 본문 중간의 신호어는 매칭하지 않는다
    ("comment.added", {"author": "some-agent",
                       "preview": "보고 접수. 이후 verify → done 흐름으로 종결됩니다"}, None),
    # 판정 필수 1 — tt-server 접수 코멘트는 신호어가 줄 시작에 와도 무시한다
    ("comment.added", {"author": "tt-server",
                       "preview": "접수됨: verify → done 흐름 안내\nreview: approve 대기"}, None),
    ("comment.added", {"author": R.TT_AGENT, "preview": "merge=abc 내 보고"}, None),
    ("comment.added", {"author": "agy", "preview": "진행 중입니다"}, None),
    # 판정 필수 2 — service.bump() 노이즈: state가 fields에 없으면 무시
    ("issue.updated", {"state": "done", "fields": ["updated_at", "blocked_detail"]}, None),
    ("issue.updated", {"state": "done"}, None),  # fields 없음 → 보수적 무시
    ("issue.updated", {"state": "in_progress", "fields": ["state"]}, None),
], ids=["other_assignee_card", "signal_mid_sentence", "tt_server_receipt", "self_comment",
        "comment_without_signal", "issue_updated_without_state_change",
        "issue_updated_state_missing_fields", "issue_updated_in_progress"])
def test_no_wake_signals(kind, payload, cache_assignee):
    row = _row(kind=kind, payload=payload)
    assert R.handle_event(row, cache=_cache(assignee=cache_assignee)) is None


def test_dispatch_entity_ignores_without_issue_fetch():
    row = _row(kind="dispatch.updated", entity="dispatch", entity_id="D-9",
               payload={"state": "done"})
    assert R.handle_event(row, cache=None) is None


# ---------- 콜드 스타트 ----------

def test_resolve_start_seq_warm_cursor_wins():
    assert R.resolve_start_seq(41, fetch_last_seq=lambda: 9999) == 41


def test_resolve_start_seq_cold_uses_last_seq():
    """판정 필수 3 — cursor 0이면 전체 재생 대신 last_seq부터."""
    assert R.resolve_start_seq(0, fetch_last_seq=lambda: 3260) == 3260


# ---------- dedup ----------

def test_dedup_ok_window():
    recent = {}
    assert R.dedup_ok(recent, "I-1", now=100.0)
    recent["I-1"] = 100.0
    assert not R.dedup_ok(recent, "I-1", now=110.0)  # 10s 후 — 차단
    assert R.dedup_ok(recent, "I-1", now=100.0 + R.DEDUP_WINDOW_S + 1)  # 창 경과 — 허용
    assert R.dedup_ok(recent, "I-2", now=110.0)  # 다른 이슈 — 허용


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


# ---------- SRJY(2026-10-17): 리뷰 stale·홀드 신호 ----------

def test_probe_delivery_hold_wakes_assignee():
    """SRJY 사건 재현 — approve 후 head가 바뀌면 probe가 hold를 남긴다."""
    row = _row(payload={"author": "probe",
                        "preview": "[tt-delivery-hold-7473525f65cc TT#M-S@a1/x] "
                                   "PR 대상/head/base 변경 — 현재 산출물로 보고 재제출·재리뷰 필요"})
    prompt = R.handle_event(row, cache=_cache())
    assert prompt is not None and "tt-delivery-hold" in prompt


def test_probe_review_re_request_wakes_assignee():
    """stale verdict → probe 재리뷰 요청도 신호다."""
    row = _row(payload={"author": "probe",
                        "preview": "[review-req #115/7941d573] 재판정 필요 → agy"})
    assert R.handle_event(row, cache=_cache()) is not None


def test_probe_needs_merge_and_high_risk_hold_wake():
    for marker in ("[needs-merge a2] 병합 불가 — 사람 판단 요청",
                   "[tt-high-risk-hold a1 #115/abc] 고위험 — 사람 병합 승인 대기"):
        row = _row(payload={"author": "probe", "preview": marker})
        assert R.handle_event(row, cache=_cache()) is not None, marker


def test_probe_review_fix_dispatch_wakes_assignee():
    row = _row(payload={"author": "probe",
                        "preview": "[tt-review-fix TT#M-S@a1/x] 재작업 위임 — 반려 사유 반영"})
    assert R.handle_event(row, cache=_cache()) is not None


def test_probe_review_start_marker_does_not_wake():
    """리뷰 '시작'은 신호가 아니다 — 완료·stale만 깨운다."""
    row = _row(payload={"author": "probe",
                        "preview": "[tt-review TT#M-S@a1/x] reviewer=agy"})
    assert R.handle_event(row, cache=_cache()) is None
