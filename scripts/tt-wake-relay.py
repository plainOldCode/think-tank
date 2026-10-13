#!/usr/bin/env python3
"""TT 이벤트 SSE → opencode TUI wake 릴레이 (M4T3TYD4-N5Q9).

GET $TT_URL/events/stream 을 구독해, 내 카드(assignee=$TT_AGENT)의 리뷰 승인·병합·
차단 신호를 $OPENCODE_TUI_URL 의 /tui/append-prompt + /tui/submit-prompt 로 주입한다.
이로써 작업 에이전트가 polling 없이 병합 완료를 알아채고 후속 조치(sync·배포)를
시작할 수 있다. TUI 미기동 시 조용히 스킵 — 15분 cron 원샷 워커가 폴백이다.

필터:
  ① comment.added + entity=issue + assignee==나 + author!=나 +
     preview에 "review: approve" | "verify → done" | "merge=" 포함
  ② issue.updated + entity=issue + assignee==나 + payload.state ∈ {done, blocked}
  멘션(message.posted)은 의도적으로 제외 — 기존 cron 원샷 워커가 소유(중복 응답 방지).

설치 (tp-13):
  cp scripts/tt-wake-relay.py ~/.local/bin/
  # ~/.config/systemd/user/tt-wake-relay.service:
  #   [Unit]
  #   Description=TT board event -> opencode TUI wake relay
  #
  #   [Service]
  #   ExecStart=/usr/bin/python3 %h/.local/bin/tt-wake-relay.py
  #   Restart=on-failure
  #   RestartSec=5
  #   Environment=TT_URL=http://100.122.96.16:7800
  #   Environment=TT_AGENT=skshim@tp-13
  #   Environment=OPENCODE_TUI_URL=http://127.0.0.1:4096
  #
  #   [Install]
  #   WantedBy=default.target
  systemctl --user daemon-reload && systemctl --user enable --now tt-wake-relay
  # TUI는 고정 포트로 기동돼야 한다: opencode --port 4096


환경변수: TT_URL(기본 mini), TT_AGENT(기본 skshim@tp-13),
  OPENCODE_TUI_URL(기본 http://127.0.0.1:4096), TT_WAKE_STATE_DIR(커서 저장),
  TT_WAKE_DRY_RUN=1(주입 대신 로그), TT_WAKE_DEBUG=1(모든 이벤트 로그)
"""

import json
import os
import re
import sys
import tempfile
import time
import urllib.request

TT_URL = os.environ.get("TT_URL", "http://100.122.96.16:7800").rstrip("/")
TT_AGENT = os.environ.get("TT_AGENT", "skshim@tp-13")
TUI_URL = os.environ.get("OPENCODE_TUI_URL", "http://127.0.0.1:4096").rstrip("/")
STATE_DIR = os.environ.get("TT_WAKE_STATE_DIR",
                           os.path.expanduser("~/.local/state/tt-wake"))
CURSOR_PATH = os.path.join(STATE_DIR, "seq")
DRY_RUN = os.environ.get("TT_WAKE_DRY_RUN") == "1"
DEBUG = os.environ.get("TT_WAKE_DEBUG") == "1"

SIGNAL_RE = re.compile(r"review: approve|verify → done|merge=", re.IGNORECASE)
WAKE_STATES = {"done", "blocked"}
ISSUE_TTL_S = 60.0
RECONNECT_S = 5.0
IDLE_TIMEOUT_S = 60  # 소켓 읽기 타임아웃 — ': ping'이 15s마다 오므로 평소 미도달


def log(msg):
    sys.stderr.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    sys.stderr.flush()


# ---------- SSE 파싱 ----------

def parse_sse(lines):
    """SSE 라인 스트림을 (seq, kind, row_dict)로. 행 단위 빈 줄로 이벤트 종결.
    ': ping' 주석과 불완전 블록은 무시한다."""
    ev_id = ev_kind = ev_data = None
    for raw in lines:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        line = raw.rstrip("\r\n")
        if not line:
            if ev_id and ev_data:
                try:
                    yield int(ev_id), ev_kind, json.loads(ev_data)
                except ValueError:
                    log("SSE parse skip: id=%r" % ev_id)
            ev_id = ev_kind = ev_data = None
            continue
        if line.startswith(":"):
            continue
        if line.startswith("id:"):
            ev_id = line[3:].strip()
        elif line.startswith("event:"):
            ev_kind = line[6:].strip()
        elif line.startswith("data:"):
            ev_data = (ev_data or "") + line[5:].strip()


# ---------- 이슈 캐시 ----------

class IssueCache:
    """GET /issues/{id} 결과를 TTL 캐시. 조회 실패 시 None(조용히 포기 — fail-open)."""

    def __init__(self, base=TT_URL, ttl=ISSUE_TTL_S):
        self.base = base
        self.ttl = ttl
        self._cache = {}

    def get(self, issue_id):
        hit = self._cache.get(issue_id)
        now = time.monotonic()
        if hit and now - hit[0] < self.ttl:
            return hit[1]
        try:
            with urllib.request.urlopen("%s/issues/%s" % (self.base, issue_id),
                                        timeout=10) as r:
                issue = json.load(r)
        except Exception as e:
            log("issue fetch fail %s: %s" % (issue_id, e))
            issue = None
        self._cache[issue_id] = (now, issue)
        return issue


def signal_prompt(issue_id, reason, detail=""):
    """TUI에 주입할 wake 프롬프트. 에이전트가 TT 보드를 확인해 후속 조치를 하도록 지시."""
    p = "[TT 웨이크] %s — %s" % (issue_id, reason)
    if detail:
        p += "\n" + detail[:300]
    p += "\nTT 보드를 확인하고 후속 조치를 진행하라 (병합 확인·mini sync·배포·카드 종결 등)."
    return p


def handle_event(row, me=TT_AGENT, cache=None):
    """이벤트 행 → wake 프롬프트 또는 None. 순수 필터 로직(테스트 대상)."""
    kind = row.get("kind")
    entity = row.get("entity")
    issue_id = row.get("entity_id")
    if entity != "issue" or not issue_id:
        return None
    try:
        payload = json.loads(row.get("payload") or "{}")
    except ValueError:
        return None
    if cache is None:
        cache = IssueCache()
    issue = cache.get(issue_id)
    if not issue or issue.get("assignee") != me:
        return None
    if kind == "comment.added":
        author = payload.get("author") or ""
        if author == me:
            return None
        preview = payload.get("preview") or ""
        if SIGNAL_RE.search(preview):
            return signal_prompt(issue_id, "코멘트 신호 (%s)" % author, preview)
        return None
    if kind == "issue.updated":
        state = payload.get("state")
        if state in WAKE_STATES:
            return signal_prompt(issue_id, "카드 상태 → %s" % state,
                                 json.dumps(payload.get("fields", []), ensure_ascii=False))
    return None


# ---------- TUI 주입 ----------

def _post_json(url, body=None, timeout=10):
    data = json.dumps(body).encode() if body is not None else b"{}"
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def inject(tui_url=TUI_URL, text="", post=None):
    """append-prompt + submit-prompt. 성공 시 True, TUI 미기동·거부 시 False."""
    post = post or _post_json
    try:
        if post("%s/tui/append-prompt" % tui_url, {"text": text}) is not True:
            log("append-prompt 거부 (TUI 없음?) url=%s" % tui_url)
            return False
        if post("%s/tui/submit-prompt" % tui_url, {}) is not True:
            log("submit-prompt 거부")
            return False
    except Exception as e:
        log("주입 실패 (TUI 미기동으로 스킵): %s" % e)
        return False
    return True


# ---------- 커서 ----------

def read_cursor():
    try:
        with open(CURSOR_PATH) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return 0


def write_cursor(seq):
    os.makedirs(STATE_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=STATE_DIR, prefix=".seq-")
    with os.fdopen(fd, "w") as f:
        f.write(str(seq))
    os.replace(tmp, CURSOR_PATH)


# ---------- 메인 루프 ----------

def open_stream(after_seq):
    req = urllib.request.Request(
        "%s/events/stream" % TT_URL,
        headers={"Last-Event-ID": str(after_seq), "Accept": "text/event-stream"})
    return urllib.request.urlopen(req, timeout=IDLE_TIMEOUT_S)


def run(cache=None, inject_fn=None, dry_run=DRY_RUN):
    cache = cache or IssueCache()
    inject_fn = inject_fn or inject
    seq = read_cursor()
    log("시작: TT_URL=%s agent=%s tui=%s cursor=%d dry_run=%s"
        % (TT_URL, TT_AGENT, TUI_URL, seq, dry_run))
    while True:
        try:
            resp = open_stream(seq)
            log("SSE 연결: after_seq=%d" % seq)
            for ev_seq, ev_kind, row in parse_sse(resp):
                if ev_seq is not None:
                    if ev_seq > seq:
                        seq = ev_seq
                        write_cursor(seq)
                if DEBUG:
                    log("event %s %s %s" % (ev_seq, ev_kind, row.get("entity_id")))
                prompt = handle_event(row, cache=cache)
                if prompt is None:
                    continue
                issue_id = row.get("entity_id")
                if dry_run:
                    log("[DRY-RUN] wake %s:\n%s" % (issue_id, prompt))
                    continue
                if inject_fn(text=prompt):
                    log("wake 주입: %s" % issue_id)
        except Exception as e:
            log("SSE 끊김, %ds 후 재접속: %s" % (RECONNECT_S, e))
            time.sleep(RECONNECT_S)


if __name__ == "__main__":
    run()
