#!/usr/bin/env python3
"""codex 실실행 스모크: dispatch → tmux → codex exec(read-only, stdin) → 마커 → 코멘트.

실 CLI 왕복 검증용(M3BZS1G3 완료조건: 실 CLI codex 1건 이상). fake TT loopback,
tmux는 별도 socket(TT_TMUX_SOCKET) — 실 서버 세션과 격리. binary는 검증된
ChatGPT.app 번들(homebrew codex-cli는 read-only sandbox 파손 — README 참조).
run-agent의 credential 격리가 실 codex 자식에서도 working인지 canary로 확인.
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

os.environ.setdefault("TT_TMUX_SOCKET", "tt-runner-smoke-codex")
os.environ["SMOKE_PLANTED_TOKEN"] = "codex-canary-7796"  # 자식 env에 나가면 안 됨
TMP = tempfile.mkdtemp(prefix="tt-runner-codex.")
os.environ["TT_RUNNER_STATE"] = os.path.join(TMP, "state")
os.environ["TT_RUNNER_SECRET"] = os.path.join(TMP, "secret")
open(os.environ["TT_RUNNER_SECRET"], "w").write("codex-smoke-secret")
os.environ["TT_RUNNER_BIND"] = "127.0.0.1"
os.environ["TT_RUNNER_PORT"] = "7798"
os.environ["TT_RUNNER_NAME"] = "runner-smoke"
os.environ["TT_AGENT"] = "runner@testbox"
ws = os.path.join(TMP, "ws")
os.makedirs(ws)
open(os.path.join(ws, "note.txt"), "w").write("PASSPHRASE-ZEBRA-42\n")
reg = os.path.join(TMP, "agents.json")
json.dump({"machine": "testbox", "agents": {
    "codex-ro": {"driver": "codex",
                 "binary": os.environ.get("CODEX_BIN",
                                          "/Applications/ChatGPT.app/Contents/Resources/codex"),
                 "permission_mode": "read", "workspace": ws, "timeout_s": 240,
                 "keep_shell": False},
}}, open(reg, "w"))
os.environ["TT_RUNNER_REGISTRY"] = reg

REPO = os.environ.get("TT_REPO", os.path.expanduser("~/Services/think-tank"))
import importlib.util as ilu
_sp = ilu.spec_from_file_location("tt_runner", os.path.join(REPO, "runner", "tt-runner.py"))
R = ilu.module_from_spec(_sp)
_sp.loader.exec_module(R)
R.TT = "http://127.0.0.1:7799"

ISSUE = {"id": "SMOKE-2", "comments": []}

class FakeTT(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("content-length", 0))
        p = json.loads(self.rfile.read(n) or b"{}")
        ISSUE["comments"].append(p)
        b = b"{}"
        self.send_response(201)
        self.end_headers()
        self.wfile.write(b)

    def log_message(self, *a):
        pass

fake = ThreadingHTTPServer(("127.0.0.1", 7799), FakeTT)
threading.Thread(target=fake.serve_forever, daemon=True).start()
runner_http = R.ThreadingHTTPServer((R.BIND, R.PORT), R.H)
threading.Thread(target=runner_http.serve_forever, daemon=True).start()
stop = threading.Event()
threading.Thread(target=R.watcher_loop, args=(stop,), daemon=True).start()

req = urllib.request.Request("http://127.0.0.1:7798/hook",
    data=json.dumps({"dispatch_id": 1, "issue_id": "SMOKE-2", "agent": "codex-ro",
                     "author": "smoke", "context": "",
                     "message": "Read note.txt in the current directory and answer with exactly the word it contains."}).encode(),
    headers={"content-type": "application/json", "authorization": "Bearer codex-smoke-secret",
             "x-tt-dispatch": "1"}, method="POST")
r = urllib.request.urlopen(req, timeout=10)
ctx = json.loads(r.read())["context"]
sess = ctx.split(":")[-1]
print("[codex-smoke] dispatched, session:", sess)

deadline = time.time() + 260
done_body = None
while time.time() < deadline:
    if any(("done exit=" in c.get("body", "")) for c in ISSUE["comments"]):
        done_body = [c["body"] for c in ISSUE["comments"] if "dispatch#1" in c["body"]][-1]
        break
    time.sleep(2)
stop.set()
runner_http.shutdown()
fake.shutdown()
R.tmux_run("kill-session", "-t", sess, capture_output=True)
R.tmux_run("kill-server", capture_output=True)

# credential 격리 확인: codex 실행 .log/.out 어디에도 canary가 없어야 한다
runtime = os.path.join(R.RUNTIME_DIR, "SMOKE-2_1")
leaked = ""
for suffix in (".log", ".out"):
    if os.path.exists(runtime + suffix):
        if "codex-canary-7796" in open(runtime + suffix).read():
            leaked += suffix
print("[codex-smoke] final comment:\n---\n" + (done_body or "(none)")[:800])
ok = bool(done_body) and "done exit=0" in done_body and "PASSPHRASE-ZEBRA-42" in done_body \
    and not leaked
if leaked:
    print("CANARY LEAKED into", leaked)
print("CODEX-SMOKE", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
