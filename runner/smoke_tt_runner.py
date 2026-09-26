#!/usr/bin/env python3
"""루프백 스모크: stdlib fake TT(dispatch payload 규격 재현) + runner + tmux(격리 socket).

mini TT 서버·실 tmux 서버에는 손대지 않는다(TT_TMUX_SOCKET 별도 서버 사용).
검증 항목 (1-8 기존, 9-14 M3BZS1G3-VNQH 확장):
 1) /hook Bearer+X-Tt-Dispatch 검증 → 즉시 200 + context 세션토큰
 2) tmux 신규 세션 → plain agent(echo) 실행 → .done/.exit 마커 → 감시자 → TT 코멘트 done
 3) 같은 dispatch_id 재투하 → DUP-SKIP (코멘트 중복 없음)
 4) bad secret → 401 / missing header → 400
 5) 파괴적 패턴 → held + 승인 요청 코멘트, '승인' dispatch → 방출·실행
 6) polling 404 → INBOX-NOT-READY 조용히 대기
 7) 계속 재지시: context=runner:testbox:tmux:<session> → send-keys 계속 경로
 8) 시크릿 로그 마스킹
 9) 가짜 CLI exit=1 + 'approval required' 출력 → done/failed가 아니라 BLOCKED 코멘트
10) 가짜 CLI exit=1 정상 크래시 출력 → failed (BLOCKED 혼동 없음)
11) workspace 오버라이드 /etc → 실행 시작 전 거부 코멘트 (장부 failed, 세션 없음)
12) agent 자식 env: *TOKEN*=심어둔 값 미노출 + secret env name 없음 (env-print CLI)
13) 무음 페이크 CLI(장시간 sleep) → STALL 코멘트(pane tail) — TIMEOUT 아님
14) STALL 후 kill 경로 → stall-killed 장부 (임계는 smoke에서 축소 주입)
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TMP = tempfile.mkdtemp(prefix="tt-runner-smoke.")
os.environ["TT_RUNNER_STATE"] = os.path.join(TMP, "state")
os.environ["TT_RUNNER_SECRET"] = os.path.join(TMP, "secret")
open(os.environ["TT_RUNNER_SECRET"], "w").write("smoke-secret-xyz")
os.environ["TT_RUNNER_BIND"] = "127.0.0.1"
os.environ["TT_RUNNER_PORT"] = "7798"
os.environ["TT_RUNNER_NAME"] = "runner-smoke"
os.environ["TT_AGENT"] = "runner@testbox"
# 감시·stall 판정을 smoke 시간에 맞게 축소 (기본값: 600/600 — README 명문화)
STALL_S = 6
STALL_K = 4
os.environ.setdefault("TT_TMUX_SOCKET", "tt-runner-smoke")
# credential 격리 실측용: 자식 env에 남아선 안 될 심볼을 부모 env에 심는다
os.environ["SMOKE_PLANTED_TOKEN"] = "planted-canary-9001"
os.environ["SMOKE_BENIGN_SETTING"] = "benign-keep"
ws = os.path.join(TMP, "ws")
os.makedirs(ws)
ghost_ws = os.path.join(TMP, "ghost-ws")  # 레지스트리 등재됐으나 실체 없는 dir — 가드가 거부해야

# 가짜 CLI 페이로드 (fake CLI 허용 — 실 CLI 왕복은 tt_smoke_codex.py / 실 dispatch)
FAKE = {}
for name, body in {
    "blocked.sh": "#!/bin/sh\necho 'ERROR: action requires approval from the user'\nexit 1\n",
    "crash.sh": "#!/bin/sh\necho 'segmentation fault, core dumped'\nexit 1\n",
    "envprint.sh": "#!/bin/sh\nenv\nexit 0\n",
    "silent.sh": "#!/bin/sh\nsleep 120\n",
}.items():
    p = os.path.join(TMP, name)
    open(p, "w").write(body)
    os.chmod(p, 0o700)

reg = os.path.join(TMP, "agents.json")
json.dump({"machine": "testbox", "agents": {
    "echo-agent": {"driver": "plain", "binary": "/bin/echo", "auto_args": [],
                   "permission_mode": "read", "workspace": ws, "timeout_s": 120,
                   "keep_shell": True, "continue_cmd": "echo CONTINUED"},
    "ghost-agent": {"driver": "plain", "binary": "/bin/echo",
                    "permission_mode": "read", "workspace": ghost_ws,
                    "allowed_workspaces": [ws, ghost_ws], "timeout_s": 120,
                    "keep_shell": False},
    "blocked-cli": {"driver": "plain", "binary": os.path.join(TMP, "blocked.sh"),
                    "workspace": ws, "timeout_s": 120, "keep_shell": False},
    "crash-cli": {"driver": "plain", "binary": os.path.join(TMP, "crash.sh"),
                  "workspace": ws, "timeout_s": 120, "keep_shell": False},
    "env-cli": {"driver": "plain", "binary": os.path.join(TMP, "envprint.sh"),
                "workspace": ws, "timeout_s": 120, "keep_shell": False},
    "silent-cli": {"driver": "plain", "binary": os.path.join(TMP, "silent.sh"),
                   "workspace": ws, "timeout_s": 1800, "keep_shell": False},
}}, open(reg, "w"))
os.environ["TT_RUNNER_REGISTRY"] = reg

REPO = os.environ.get("TT_REPO", os.path.expanduser("~/Services/think-tank"))
sys.path.insert(0, os.path.join(REPO, "server"))
from work_contract import current_contract
CONTRACT = current_contract()
import importlib.util as ilu
_sp = ilu.spec_from_file_location("tt_runner", os.path.join(REPO, "runner", "tt-runner.py"))
R = ilu.module_from_spec(_sp)
_sp.loader.exec_module(R)
R.STALL_SILENCE_S = STALL_S
R.STALL_KILL_AFTER_S = STALL_K
R.WATCH_INTERVAL_S = 1
os.environ["TT_URL"] = "http://127.0.0.1:7799"
R.TT = "http://127.0.0.1:7799"

# ---- fake TT 서버 ----
ISSUE = {"id": "SMOKE-1", "title": "smoke", "comments": []}
_next = [0]

class FakeTT(BaseHTTPRequestHandler):
    def _j(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path == "/issues/SMOKE-1":
            return self._j(200, dict(ISSUE))
        return self._j(404, {"detail": "not found"})  # /agents/.../pending 포함

    def do_POST(self):
        n = int(self.headers.get("content-length", 0))
        p = json.loads(self.rfile.read(n) or b"{}")
        if self.path == "/issues/SMOKE-1/comments":
            ISSUE["comments"].append(p)
            return self._j(201, p)
        return self._j(404, {"detail": "nf"})

    def log_message(self, *a):
        pass

fake = ThreadingHTTPServer(("127.0.0.1", 7799), FakeTT)
threading.Thread(target=fake.serve_forever, daemon=True).start()

# ---- runner 기동 ----
runner_http = R.ThreadingHTTPServer((R.BIND, R.PORT), R.H)
threading.Thread(target=runner_http.serve_forever, daemon=True).start()
stop = threading.Event()
threading.Thread(target=R.watcher_loop, args=(stop,), daemon=True).start()

def hook(payload, secret="smoke-secret-xyz", header=True):
    req = urllib.request.Request("http://127.0.0.1:7798/hook",
        data=json.dumps(payload).encode(),
        headers={"content-type": "application/json", "authorization": "Bearer " + secret,
                 **({"x-tt-dispatch": str(payload["dispatch_id"])} if header else {})},
        method="POST")
    try:
        r = urllib.request.urlopen(req, timeout=10)
        return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, {}

def wait_for(pred, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.5)
    return False

def comments_of(did):
    return [c["body"] for c in ISSUE["comments"] if "dispatch#%d" % did in c["body"]]

results = {}

# 1) 정상 dispatch (서버 deliver payload와 동일 필드 구성)
_next[0] += 1
st, body = hook({"dispatch_id": _next[0], "issue_id": "SMOKE-1", "issue_title": "smoke",
                 "agent": "echo-agent", "author": "smoke", "message": "hello-runner-test",
                 "context": "", "comments": [], "tt_url": "http://127.0.0.1:7799",
                 "work_contract": CONTRACT, "execution_attempt": 7})
results["immediate200"] = st == 200
results["context_token"] = body.get("context", "").startswith("runner:testbox:tmux:")
sess = body.get("context", "").split(":")[-1]
wait_for(lambda: any("done exit=0" in b for b in comments_of(_next[0])), 30)
done_cs = comments_of(_next[0])
results["e2e_done"] = any("done exit=0" in b and sess in b for b in done_cs)
results["echo_in_comment"] = any("hello-runner-test" in b for b in done_cs)
results["tmux_session_seen"] = R.tmux_run("has-session", "-t", sess).returncode == 0
with open(os.path.join(R.RUNTIME_DIR, "SMOKE-1_%d.log" % _next[0])) as f:
    actual_prompt = f.read()
results["contract_in_new_process"] = (CONTRACT["version"] in actual_prompt
    and CONTRACT["instructions"] in actual_prompt and "execution_attempt=7" in actual_prompt)
print("[1] dispatch1", st, body, "| comments:", len(done_cs))

# 2) DUP-SKIP (같은 dispatch_id 재투하)
before = len(ISSUE["comments"])
st2, _ = hook({"dispatch_id": _next[0], "issue_id": "SMOKE-1",
               "agent": "echo-agent", "message": "hello-runner-test", "context": ""})
time.sleep(1.5)
log = open(os.path.join(R.STATE_DIR, "runner.log")).read()
results["dup_skip"] = st2 == 200 and "DUP-SKIP" in log and len(ISSUE["comments"]) == before
print("[2] dup", st2)

# 3) 보안
st3, _ = hook({"dispatch_id": 99, "issue_id": "SMOKE-1", "agent": "echo-agent",
               "message": "x"}, secret="wrong")
st4, _ = hook({"dispatch_id": 98, "issue_id": "SMOKE-1", "agent": "echo-agent",
               "message": "x"}, header=False)
results["bad_secret_401"] = st3 == 401
results["no_header_400"] = st4 == 400
print("[3] 401/400", st3, st4)

# 4) 파괴적 게이트 → held → '승인' 방출
_next[0] += 1
st5, _ = hook({"dispatch_id": _next[0], "issue_id": "SMOKE-1", "agent": "echo-agent",
               "message": "rm -rf the tmp please", "context": ""})
time.sleep(1.5)
key = "SMOKE-1#%d" % _next[0]
ent = R.load_runs().get(key, {})
results["gate_hold"] = ent.get("status") == "held"
_next[0] += 1
hook({"dispatch_id": _next[0], "issue_id": "SMOKE-1", "agent": "echo-agent",
      "message": "승인", "context": ""})
wait_for(lambda: R.load_runs().get(key, {}).get("status") == "done", 40)
ent = R.load_runs().get(key, {})
results["gate_release_run"] = ent.get("status") == "done" and ent.get("approved") is True
print("[4] gate", ent.get("status"))

# 5) 계속 재지시: 같은 세션 context로 새 dispatch → send-keys 계속 경로
_next[0] += 1
st6, body6 = hook({"dispatch_id": _next[0], "issue_id": "SMOKE-1", "agent": "echo-agent",
                   "message": "follow-up instruction", "context": R.ctx_token(sess),
                   "work_contract": CONTRACT, "execution_attempt": 7})
time.sleep(1.5)
ent3 = R.load_runs().get("SMOKE-1#%d" % _next[0], {})
results["continue_path"] = (body6.get("context") == R.ctx_token(sess)
                            and ent3.get("mode") == "continue" and ent3.get("session") == sess)
wait_for(lambda: R.load_runs().get("SMOKE-1#%d" % _next[0], {}).get("status") == "done", 20)
with open(os.path.join(R.RUNTIME_DIR, "SMOKE-1_%d.log" % _next[0])) as f:
    actual_resume = f.read()
results["contract_in_resumed_process"] = (CONTRACT["version"] in actual_resume
    and CONTRACT["instructions"] in actual_resume and "follow-up instruction" in actual_resume)
print("[5] continue", body6.get("context"), ent3.get("mode"))

# 6) polling 404 → INBOX-NOT-READY
R.poll_once()
log = open(os.path.join(R.STATE_DIR, "runner.log")).read()
results["inbox_not_ready"] = "INBOX-NOT-READY" in log

# 7) 시크릿 로그 마스킹
results["secret_not_in_log"] = "smoke-secret-xyz" not in log

# ---- M3BZS1G3-VNQH 확장 ----
# 9) 입력필요 가짜 CLI → BLOCKED
_next[0] += 1
did = _next[0]
hook({"dispatch_id": did, "issue_id": "SMOKE-1", "agent": "blocked-cli",
      "message": "do the thing", "context": ""})
ok = wait_for(lambda: any(" BLOCKED " in b for b in comments_of(did)), 40)
ent = R.load_runs().get("SMOKE-1#%d" % did, {})
results["blocked_classified"] = ok and ent.get("status") == "blocked" and "waiting_for=human" in " ".join(comments_of(did))
print("[9] blocked", ent.get("status"), ent.get("blocked_on"))

# 10) 크래시 → failed (BLOCKED 아님)
_next[0] += 1
did = _next[0]
hook({"dispatch_id": did, "issue_id": "SMOKE-1", "agent": "crash-cli",
      "message": "do the thing", "context": ""})
ok = wait_for(lambda: any(" failed exit=1" in b for b in comments_of(did)), 40)
ent = R.load_runs().get("SMOKE-1#%d" % did, {})
results["crash_stays_failed"] = ok and ent.get("status") == "failed" \
    and not any(" BLOCKED " in b for b in comments_of(did))
print("[10] crash", ent.get("status"))

# 11) workspace 불변식: 레지스트리에 등재된 workspace가 실제로 없는 디렉 → 실행 시작 전 거부
_next[0] += 1
did = _next[0]
hook({"dispatch_id": did, "issue_id": "SMOKE-1", "agent": "ghost-agent",
      "message": "list the dir", "context": ""})
ok = wait_for(lambda: any("불변식 위반" in b for b in comments_of(did)), 30)
ent = R.load_runs().get("SMOKE-1#%d" % did, {})
results["workspace_guard"] = ok and ent.get("status") == "failed" \
    and str(ent.get("detail", "")).startswith("workspace-guard")
print("[11] ws-guard", ent.get("status"), ent.get("detail"))

# 12) credential 격리: env-print CLI 출력에 심은 토큰·secret env가 없어야 한다
_next[0] += 1
did = _next[0]
hook({"dispatch_id": did, "issue_id": "SMOKE-1", "agent": "env-cli",
      "message": "print env", "context": ""})
wait_for(lambda: R.load_runs().get("SMOKE-1#%d" % did, {}).get("status") == "done", 40)
out = open(os.path.join(R.RUNTIME_DIR, "SMOKE-1_%d.log" % did)).read()
results["env_canary_dropped"] = "planted-canary-9001" not in out and out.strip() != ""
results["env_benign_kept"] = "benign-keep" in out
print("[12] env-iso canary=%s benign=%s" % (results["env_canary_dropped"], results["env_benign_kept"]))

# 13) 무음 stall: silent CLI → STALL 코멘트 (TIMEOUT 아님)
_next[0] += 1
did = _next[0]
hook({"dispatch_id": did, "issue_id": "SMOKE-1", "agent": "silent-cli",
      "message": "hang quietly", "context": ""})
ok = wait_for(lambda: any(" STALL " in b for b in comments_of(did)), STALL_S + 40)
results["stall_notified"] = ok
ent = R.load_runs().get("SMOKE-1#%d" % did, {})
print("[13] stall notified:", ok)

# 14) STALL 후 계속 무음 → kill + stall-killed 장부
ok = wait_for(lambda: R.load_runs().get("SMOKE-1#%d" % did, {}).get("detail") == "stall-killed",
              STALL_K + 40)
results["stall_killed"] = ok and R.tmux_run("has-session", "-t", ent.get("session", "no-session")).returncode != 0
print("[14] stall killed:", results["stall_killed"])

print("[res]", json.dumps(results, ensure_ascii=False))

stop.set()
runner_http.shutdown()
fake.shutdown()
R.tmux_run("kill-session", "-t", sess, capture_output=True)
R.tmux_run("kill-server", capture_output=True)
ok = all(results.values())
print("SMOKE", "PASS" if ok else "FAIL", [k for k, v in results.items() if not v])
sys.exit(0 if ok else 1)
