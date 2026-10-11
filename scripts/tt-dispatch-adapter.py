#!/usr/bin/env python3
"""TT dispatch -> Hermes Kanban adapter (TT issue M38W9680-8H7Q).

dispatch는 즉시 실행이 아니라 즉시 브릿징: 이 수신기는 TT 서버의 dispatch webhook을
받아 ①secret+헤더 검증 후 200 즉시 회신 ②ledger 조회 — 매핑된 이슈면 Kanban 카드에
comment 이식(steer), 미매핑이면 pick_board 규칙으로 즉시 카드 생성(idempotency) +
ledger 등록 + TT backlink 코멘트. dispatch는 TT 쪽 close/done을 절대 유발하지 않는다.

tt-bridge.py의 함수(tt/kPick_board/ledger/kanban)를 import해 재사용하므로 보드 규칙·
장부 포맷은 브리지와 단일 소스. ledger state="dispatch" 엔트리는 phase_a reconcile이
건드리지 않는다(open/watch만 처리) — 브리지 크론과 병행 무충돌.

바인딩: tailnet IP 전용 — TT_ADAPTER_BIND 환경변수(launchd plist)에서 지정. 0.0.0.0 바인딩 금지.
secret: ~/.hermes/tt-adapter.secret (chmod 600), TT /agents 등록 시 같은 값.
"""
import importlib.util
import json
import traceback
import os
import re
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BRIDGE_PATH = os.path.expanduser("~/bin/tt-bridge.py")
SECRET_PATH = os.path.expanduser("~/.hermes/tt-adapter.secret")
LOG_PATH = os.path.expanduser("~/.hermes/tt-dispatch-adapter.log")
BIND = os.environ.get("TT_ADAPTER_BIND", "")
PORT = int(os.environ.get("TT_ADAPTER_PORT", "7795"))

_spec = importlib.util.spec_from_file_location("tt_bridge", BRIDGE_PATH)
bridge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bridge)


def tt_comment(tid, body):
    bridge.tt_raw("POST", "/issues/%s/comments" % tid, {"author": bridge.AGENT, "body": body})


def log(msg):
    line = "%s %s" % (time.strftime("%F %T"), msg)
    try:
        with open(LOG_PATH, "a") as f:
            f.write(line.strip("\n") + "\n")
    except OSError:
        pass


def adapter_secret():
    try:
        with open(SECRET_PATH) as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""


def ledger_merge(new_entries):
    """브리지 크론과 같은 파일을 쓰므로 저장 직전 재학습(merge)로 Lost-update 방지."""
    led = bridge.load_ledger()
    led.update(new_entries)
    bridge.save_ledger(led)


def card_status(ent):
    """실시간 카드 상태 조회 (parse failure → None=steer 보수적 유지)."""
    r = bridge.kanban(["--board", ent["board"], "show", ent["task_id"]])
    m = re.search(r"^\s*status:\s+(\S+)", r.stdout or "", re.M)
    return m.group(1) if m else None


# ETHX: dispatch message의 명시적 상태 지시 — 데이터 취급(V587 dispatch#38 사고:
# human 지시 'done 가능' vs 고정 footer '생명주기 독립' 충돌 → 워커가 footer 준수,
# TT done/verify 미제출). #state: 마커가 정식 데이터형, 자연어 핵심구는 보조 탐지.
STATE_MARKER_RE = re.compile(r"^#\s*state\s*[:：]", re.I)
STATE_HINT_RE = re.compile(
    r"(done\s*가능|done\s*해도|done으로\s*마감|tt\s*done\b|verify\s*제출|"
    r"반납만|close\s*금지|done\s*금지|상태\s*변경\s*금지)", re.I)


def _state_directive(msg):
    """명시적 상태 지시 추출 (ETHX 리뷰 R2/R3 반영).
    ① #state: 마커 — 자연어 배경 서술보다 항상 우선, 복수 마커는 모두 인용(충돌 노출)
    ② 마커가 없으면 자연어 핵심구 라인(첫 일치)
    반환: (지시 문자열|None, 마커여부) — 지시 없는 dispatch는 기존 footer 그대로."""
    lines = [l.strip() for l in (msg or "").splitlines() if l.strip()]
    markers = [l for l in lines if STATE_MARKER_RE.match(l)]
    if markers:
        return " ; ".join(markers), True
    for s in lines:
        if not s.startswith("#") and STATE_HINT_RE.search(s):
            return s, False
    return None, False


def _directive_quote(text, is_marker, limit=200):
    """지시 핵심이 인용에 살아남도록 절단 (리뷰 R3): 마커는 값이 앞에 오므로 앞 절단,
    자연어 라인은 핵심구 매치 위치 중심 윈도우 — 배경 서술이 지시를 밀어내지 않는다."""
    if len(text) <= limit:
        return text
    if not is_marker:
        m = STATE_HINT_RE.search(text)
        if m:
            start = max(0, m.start() - 60)
            end = min(len(text), m.end() + 120)
            return ("… " if start > 0 else "") + text[start:end] + (" …" if end < len(text) else "")
    return text[:limit] + "…"


def build_dispatch_body(p, issue, did, board, task_hint=None):
    """dispatch 카드 본문 조립 (2026-09-30 사용자 규약): 지시문만 이식하던 과거와 달리
    ① TT 본문 verbatim(출처 표기) ② TT 왕복 3종 규약 ③ 실행 경계를 본문에 고정.
    카드를 triage로 넣는 어댑터가 계약 원천(TT)을 본문에서 누락시키면 워커가 지시문만 보고 방황한다."""
    msg = (p.get("message") or "").strip()
    tt_body = (issue.get("body") or "").strip()
    if len(tt_body) > 20000:
        tt_body = tt_body[:20000] + "\n\n(TT 본문 20000자에서 절단 — 원문 원하시면 `tt show %s` 참조)" % issue.get("id")
    labels = ", ".join(issue.get("labels") or []) or "-"
    state = issue.get("state", "?")
    tid = str(issue.get("id"))
    directive, is_marker = _state_directive(msg)
    if directive:
        # ETHX: 지시가 있으면 독립 규약 bullet을 '지시 우선'으로 치환하고 규약 2의 tt done도
        # 지시 조건부로 전환(리뷰 R1 — done 금지·반납만 지시에 고정 완료 단계 잔존 제거).
        # 우선순위는 본문 한 곳(실행 경계)에만 존재(수용기준 3). 지시 없으면 기존 문구 그대로.
        quote = _directive_quote(directive, is_marker)
        boundary = (
            "- **상태 지시 우선 (dispatch 지시 — 이 카드 한정)**: 이 카드의 상태 지시는 "
            "「%s」이다. 이 지시는 위 독립 규약과 완료 단계(규약 2의 tt done/verify 제출)보다 "
            "우선한다 — done/verify를 요구하면 카드 완료 후 TT 보고(`tt note %s` + `tt done %s "
            "--report report.json --json` 또는 verify)까지 수행하고, done 금지·반납만이면 "
            "tt done/verify 없이 결과 코멘트로 종결한다. TT 본문과의 충돌은 TT 코멘트로 기록한다."
            % (quote, tid, tid))
        rule2 = (
            "2. 완료 시 ① TT에 결과 코멘트(`tt note %s \"...\"`, 카드 코멘트의 종합 내용 포함) "
            "② **상태 지시(아래 실행 경계)가 tt done/verify를 요구할 때만** `tt done %s --report report.json --json` "
            "— report의 contract_version/attempt는 claim 응답 원본 복사, result는 passed/failed/inconclusive enum"
            "(서술은 evidence에), method tdd면 red_* 필수. 지시가 done 금지·반납만이면 tt done/verify 없이 "
            "결과 코멘트로 종결한다(지시상 미제출 — 게이트 우회 아님). 실패/불확실 보고는 review에 남는다."
            % (tid, tid))
    else:
        boundary = ("- TT 이슈 상태와 카드 생명주기는 독립(브리지 reconcile이 회차 동기화). "
                    "dispatch 지시가 TT 본문과 충돌하면 카드는 dispatch 지시를 따르되 "
                    "TT 코멘트에 충돌 사실을 남긴다.")
        rule2 = (
            "2. 완료 시 ① TT에 결과 코멘트(`tt note %s \"...\"`, 카드 코멘트의 종합 내용 포함) "
            "② `tt done %s --report report.json --json` — report의 contract_version/attempt는 claim 응답 원본 복사, "
            "result는 passed/failed/inconclusive enum(서술은 evidence에), method tdd면 red_* 필수, 대체면 reason. "
            "실패/불확실 보고는 review에 남는다." % (tid, tid))
    parts = [
        "## 지시 (dispatch#%s, %s)" % (did, p.get("author", "?")),
        msg or "(지시문 없음 — TT 본문 참조)",
        "",
        "## TT 왕복 규약 (어댑터 고정, 9/28~9/30 결정 — 3종 누락 금지; decomposer는 본문 앞 4000자만 본다)",
        "1. 작업 시작 시 `tt claim %s` 또는 lease 연장(`tt heartbeat %s`). 상태가 todo가 아니거나 타 에이전트 보유(409/steal 거부)면 사유를 이 카드에 코멘트하고 kanban_block — 우회 금지." % (tid, tid),
        rule2,
        "3. 카드 complete(또는 실패 시 kanban_block) 후 장부 회신 확인. root는 자식 결과 집계만 — root가 worker를 겸하지 않는다(9/30 사용자 규약).",
        "",
        "## 실행 경계",
        boundary,
        "- 코드 변경 산출물은 브랜치 push까지가 아니라 **push + PR 생성(`gh pr create --base main --head <브랜치>`, 본문에 tt#ID/카드 id 출처 사슬 고정)까지가 완료**다(TT 규약 확장 2026-09-30 — t_9d8875d4 PR 누락 교훈). merge/push-to-main은 여전히 사용자 결정.",
        "",
        "## TT 이슈 원문 (출처: think-tank %s, state=%s, labels=%s)" % (tid, state, labels),
        tt_body or "(TT 본문 없음)",
        "",
        "Source: think-tank dispatch → Kanban 어댑터 (TT M38W9680-8H7Q). board=%s" % board,
    ]
    if task_hint:
        parts.append("prev_card=%s" % task_hint)
    return "\n".join(parts)


def handle_dispatch(p):
    did, tid = p["dispatch_id"], str(p["issue_id"])
    led = bridge.load_ledger()
    ent = led.get(tid)
    if isinstance(ent, dict) and ent.get("state") in ("open", "watch", "dispatch"):
        # steer: 이미 매핑된 이슈 — 단 카드가 terminal(done/archived/cancelled)이면
        # 코멘트만 박히고 아무도 안 깨는 침묵 실패가 된다 (dispatch#37 사고, 2026-09-26:
        # done 카드에 steer 'ok' → 새 run 0건). terminal이면 새 카드로 승격한다.
        st = card_status(ent)
        if st not in ("done", "archived", "cancelled"):
            r = bridge.kanban(["--board", ent["board"], "comment", ent["task_id"],
                               "dispatch#%s from %s: %s" % (did, p.get("author", "?"), p.get("message", ""))])
            tt_comment(tid, "kanban steer → %s/%s dispatch#%s %s" % (
                ent["board"], ent["task_id"], did, "ok" if r.returncode == 0 else ("FAIL rc=%d %s" % (r.returncode, r.stderr[:120]))))
            log("STEER tt#%s -> %s/%s dispatch#%s rc=%d" % (tid, ent["board"], ent["task_id"], did, r.returncode))
            return
        log("STEER-TERMINAL tt#%s card %s/%s status=%s → 새 카드 승격 dispatch#%s" % (
            tid, ent["board"], ent["task_id"], st, did))
        prev_task = "%s/%s(%s)" % (ent["board"], ent["task_id"], st)
        led.pop(tid, None)  # 매핑 갱신: 아래 생성 경로가 새 카드로 재등록
        bridge.save_ledger(led)
    else:
        prev_task = None

    issue = bridge.tt_raw("GET", "/issues/" + tid)
    if not isinstance(issue, dict) or "id" not in issue:
        log("DROP tt#%s: issue fetch failed" % tid)
        return
    board = None
    route_src = None
    # LLM 판정 경로 토글: env(TT_BOARD_LLM=1) 또는 marker 파일(~/.hermes/tt-board-llm.enabled).
    # launchd 캐시 정의로 env 변경 반영이 번거로운 환경(게이트웨이 안에서 bootout 차단)을 위한 대안 경로.
    llm_on = os.environ.get("TT_BOARD_LLM") == "1" or os.path.exists(
        os.path.expanduser("~/.hermes/tt-board-llm.enabled"))
    if llm_on:
        b, conf, reason, src = bridge.pick_board_llm(issue)
        if b and conf != "low":
            board = b
            route_src = "llm:" + src
            log("ROUTE-LLM tt#%s -> %s (%s/%s: %s)" % (tid, b, conf, src, reason))
        else:
            log("ROUTE-LLM-MISS tt#%s (%s/%s: %s/%s) — scoring fallback" % (tid, b, conf, src, reason))
    if not board:
        board = bridge.pick_board(issue)
        route_src = "scoring"
    if board:
        bridge.log_routing_feedback(issue, board, route_src)
    if not board:
        try:
            scores = bridge.board_scores(issue)
            ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))[:3]
        except Exception:
            ranked = []
        hint = (" 유력 후보: " + ", ".join("%s=%d" % kv for kv in ranked)) if ranked else ""
        tt_comment(tid, "kanban 어댑터: dispatch#%s — 보드 라우팅 불일치%s. 카드 본문에 보드명/키워드 지정 또는 EXPLICIT_BOARD 추가 필요." % (did, hint))
        log("ROUTE-NONE tt#%s dispatch#%s ranked=%s" % (tid, did, ranked))
        return
    try:
        log("ROUTE tt#%s -> %s scores=%s" % (tid, board, sorted(bridge.board_scores(issue).items(), key=lambda kv: -kv[1])[:3]))
    except Exception:
        pass

    key = "tt-%s-dispatch-%s" % (tid, did)
    title = "[tt#%s] %s" % (tid, (p.get("issue_title") or issue.get("title") or "")[:100])
    body = build_dispatch_body(p, issue, did, board, task_hint=prev_task)
    args = ["--board", board, "create", title, "--body", body, "--triage",
            "--assignee", bridge.ASSIGNEE, "--idempotency-key", key, "--json"]
    ws = bridge.pick_workspace(issue)
    if ws:
        args += ["--workspace", "dir:" + ws]
    r = bridge.kanban(args)
    try:
        task_id = json.loads(r.stdout[r.stdout.find("{"):])["id"]
    except Exception:
        tt_comment(tid, "kanban 어댑터: dispatch#%s 카드 생성 실패 board=%s rc=%d %s" % (did, board, r.returncode, r.stderr[:150]))
        log("CREATE-FAIL tt#%s dispatch#%s rc=%d %s" % (tid, did, r.returncode, r.stderr[:200]))
        return
    # idempotency 재현(재실행/중복 수신)이면 같은 id가 반환된다 — ledger는 최초 1회만 등록
    if led.get(tid) is None:
        ledger_merge({tid: {"board": board, "task_id": task_id, "state": "dispatch",
                            "origin": "dispatch#%s" % did}})
    # mother 규약(2026-09-28 사용자 결정): dispatch로 mother 카드를 만든 그 순간 TT claim.
    # (bridge phase_a가 10분마다 claim-or-lease로 회수·연장하지만, 첫 10분 공백과
    #  타 에이전트 선점 창을 없애기 위해 생성 경로에서 즉시 시도 — 실패해도 카드는 유효.)
    try:
        ok, why = bridge.acquire_lease(tid)
        log("MOTHER-CLAIM tt#%s %s: %s" % (tid, "ok" if ok else "skip", why))
    except Exception as e:
        log("MOTHER-CLAIM-ERR tt#%s: %s: %s" % (tid, type(e).__name__, e))
    tt_comment(tid, "kanban:%s/%s dispatch#%s → triage 카드 생성/확인 (어댑터, idem=%s). dispatch는 TT 상태를 바꾸지 않는다 — mother claim만 bridge가 수행(규약 9/28, dispatch-왕복)." % (board, task_id, did, key))
    log("CREATE tt#%s -> %s/%s dispatch#%s" % (tid, board, task_id, did))


class H(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            return self._send(200, {"ok": True, "name": "tt-dispatch-adapter",
                                    "bind": BIND, "port": PORT})
        return self._send(404, {"detail": "not found"})

    def do_POST(self):
        if self.path != "/hook":
            return self._send(404, {"detail": "not found"})
        sec = adapter_secret()
        auth = (self.headers.get("authorization") or "").replace("Bearer ", "")
        if sec and auth != sec:
            log("REJECT 401 from %s" % self.client_address[0])
            return self._send(401, {"detail": "bad secret"})
        if not self.headers.get("x-tt-dispatch"):
            return self._send(400, {"detail": "missing X-Tt-Dispatch header"})
        try:
            n = int(self.headers.get("content-length", 0))
            p = json.loads(self.rfile.read(n).decode())
            for k in ("dispatch_id", "issue_id", "message"):
                if k not in p:
                    raise ValueError("missing " + k)
        except Exception as e:
            return self._send(400, {"detail": "bad payload: %s" % e})
        threading.Thread(target=self._safe, args=(p,), daemon=True).start()
        return self._send(200, {"context": "adapter:%s" % p["issue_id"]})

    def _safe(self, p):
        try:
            handle_dispatch(p)
        except Exception as e:
            log("ERROR dispatch#%s: %s: %s\n%s" % (p.get("dispatch_id"), type(e).__name__, e, traceback.format_exc()))

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    if not BIND:
        log("FATAL TT_ADAPTER_BIND 미설정 — launchd plist EnvironmentVariables 또는 export로 지정")
        raise SystemExit(2)
    log("START bind=%s:%d" % (BIND, PORT))
    ThreadingHTTPServer((BIND, PORT), H).serve_forever()
