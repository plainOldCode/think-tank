#!/usr/bin/env python3
"""E2E 스모크 (TT M3BZS1FS-5722): 서버+러너 실 프로세스 왕복 실증.

실증 2건:
 A. reconcile release — dispatch→tmux 실행 중 카드 done 전이 → 서버 release 명령 →
    러너 tmux kill-session + 장부 cancelled + 왕복 댓글 확인.
 B. why-blocked — waiting_for=dependency 차단 → tt 왜-blocked( 누락) → 의존 done →
    release_ready=true + [release-ready] 댓글 1회.

실행: /path/.venv/bin/python scripts/smoke_5722.py  (repo root 기준; tmp DB/socket 사용,
실 launchd 서비스와 무충돌. tmux 세션은 tt- 접두 demo 전용만 생성/kill.)
"""
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
SERVER_PORT = int(os.environ.get("SMOKE_PORT", "7811"))
RUNNER_PORT = int(os.environ.get("SMOKE_RUNNER_PORT", "7799"))
TT = f"http://127.0.0.1:{SERVER_PORT}"
SECRET = "smoke-5722-secret"


def http(method, path, payload=None, base=TT, timeout=15):
    data = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            return r.status, (json.loads(body) if body else None)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")
    except Exception as e:  # 기동 대기 중 connection refused 허용
        return 0, {"error": "%s: %s" % (type(e).__name__, e)}


def log(msg):
    print(time.strftime("%H:%M:%S") + " " + msg, flush=True)


def wait(cond, limit=30, why=""):
    t0 = time.time()
    while time.time() - t0 < limit:
        if cond():
            return True
        time.sleep(0.5)
    raise SystemExit(f"TIMEOUT waiting: {why}")


def main():
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="smoke5722."))
    (tmp / "state").mkdir()
    (tmp / "runs").mkdir()
    (tmp / "inbox").mkdir()
    (tmp / "secret").write_text(SECRET)
    # tmux socket 격리: 실 launchd 러너와 같은 서버를 쓰면 안 된다 — PATH shim으로
    # 모든 tmux 호출에 -L smoke5722를 강제 (러너는 bare tmux 호출, master 기준).
    sock = "smoke5722"
    (tmp / "bin").mkdir()
    shim = tmp / "bin" / "tmux"
    shim.write_text('#!/bin/sh\nexec /opt/homebrew/bin/tmux -L %s "$@"\n' % sock)
    shim.chmod(0o755)

    def tmux_has(name):
        r = subprocess.run(["/opt/homebrew/bin/tmux", "-L", sock, "has-session", "-t", name],
                           capture_output=True)
        return r.returncode == 0

    sleeper = tmp / "sleeper.sh"
    sleeper.write_text("#!/bin/sh\nsleep 60\n")
    sleeper.chmod(0o755)
    (tmp / "agents.json").write_text(json.dumps({"machine": "smokebox", "agents": {
        "sleeper": {"driver": "plain", "binary": str(sleeper), "workspace": str(tmp)}}}))
    env_r = dict(os.environ,
                 TT_RUNNER_STATE=str(tmp / "state"),
                 TT_RUNNER_SECRET=str(tmp / "secret"),
                 TT_RUNNER_REGISTRY=str(tmp / "agents.json"),
                 TT_RUNNER_BIND="127.0.0.1", TT_RUNNER_PORT=str(RUNNER_PORT),
                 TT=TT, TT_URL=TT, PYTHONPATH=str(ROOT / "server"),
                 PATH=str(tmp / "bin") + ":" + os.environ.get("PATH", ""))
    env_s = dict(os.environ, TT_DB=str(tmp / "tt.db"), PYTHONPATH=str(ROOT / "server"))
    srv = subprocess.Popen([sys.executable, "-m", "uvicorn", "app:app",
                            "--host", "127.0.0.1", "--port", str(SERVER_PORT)],
                           cwd=str(ROOT / "server"), env=env_s,
                           stdout=open(tmp / "server.log", "w"), stderr=subprocess.STDOUT)
    run = subprocess.Popen([sys.executable, str(ROOT / "runner" / "tt-runner.py"), "serve"],
                           env=env_r, stdout=open(tmp / "runner.log", "w"), stderr=subprocess.STDOUT)
    procs = [srv, run]
    try:
        wait(lambda: http("GET", "/health")[0] == 200, 20, "server up")
        wait(lambda: http("GET", "/health", base=f"http://127.0.0.1:{RUNNER_PORT}")[0] == 200,
             20, "runner up")
        log("SMOKE-START server=%d runner=%d tmp=%s" % (SERVER_PORT, RUNNER_PORT, tmp))

        # ---- A. reconcile release 왕복 ----
        st, ag = http("POST", "/agents", {"name": "sleeper", "secret": SECRET,
                                          "base_url": f"http://127.0.0.1:{RUNNER_PORT}/hook",
                                          "release_hook": True})
        assert st == 201 and ag["release_hook"] == 1, ag
        st, issue = http("POST", "/issues", {"title": "E2E-5722-A reconcile release 왕복"})
        iid = issue["id"]
        http("POST", f"/issues/{iid}/claim", {"agent": "smoke@studio"})
        st, d = http("POST", f"/issues/{iid}/dispatch",
                     {"agent": "sleeper", "message": "60초 잠들기(실증용)", "author": "smoke"})
        assert st == 201 and d["status"] == "ok", d
        sess = "tt-sleeper-%s" % d["id"]
        wait(lambda: tmux_has(sess), 20, f"tmux {sess} start")
        log("A1 dispatch#%s → tmux %s 실행 확인 (running)" % (d["id"], sess))
        st, done = http("PATCH", f"/issues/{iid}", {"state": "done"})
        assert st == 200, done
        log("A2 카드 done 전이 → 서버 release 발송 (transition ok)")

        def release_back():
            _, got = http("GET", f"/issues/{iid}")
            bodies = [c["body"] for c in got["comments"]]
            return (any(c["author"] == "tt-server" and "reconcile release → sleeper: ok" in c["body"]
                        for c in got["comments"])
                    and any("cancelled — 카드 terminalize" in b for b in bodies))
        wait(release_back, 25, "release 왕복 댓글")
        wait(lambda: not tmux_has(sess), 15, f"tmux {sess} kill")
        ledger = json.loads((tmp / "state" / "runs.json").read_text())
        ent = ledger["%s#%s" % (iid, d["id"])]
        assert ent["status"] == "cancelled" and ent["detail"] == "release-command", ent
        log("A3 수렴 확인: tmux 세션 소멸 + 장부 %s/%s + 왕복 댓글 PASS" %
            (ent["status"], ent["detail"]))

        # ---- B. why-blocked 왕복 ----
        st, dep = http("POST", "/issues", {"title": "E2E-5722-B 선행 작업"})
        st, blk = http("POST", "/issues", {"title": "E2E-5722-B 막힌 카드"})
        st, b2 = http("PATCH", "/issues/%s" % blk["id"],
                      {"state": "in_progress", "assignee": "smoke@studio"})
        st, b3 = http("PATCH", "/issues/%s" % blk["id"],
                      {"state": "blocked", "waiting_for": "dependency",
                       "waiting_actor": "smoke@studio",
                       "blocked_detail": "의존: %s" % dep["id"]})
        assert st == 200 and b3["waiting_for"] == "dependency", b3
        st, w = http("GET", "/issues/%s/why-blocked" % blk["id"])
        assert st == 200 and w["release_ready"] is False and w["missing"] == [dep["id"]], w
        log("B1 blocked(waiting_for=dependency) → why-blocked: missing=%s release_ready=False" %
            ",".join(w["missing"]))
        http("POST", "/issues/%s/claim" % dep["id"], {"agent": "smoke@studio"})
        st, _ = http("PATCH", "/issues/%s" % dep["id"], {"state": "done"})
        assert st == 200
        st, w2 = http("GET", "/issues/%s/why-blocked" % blk["id"])
        assert w2["release_ready"] is True and w2["missing"] == [], w2
        _, got = http("GET", "/issues/%s" % blk["id"])
        rr = [c for c in got["comments"] if c["author"] == "tt-server" and "release-ready" in c["body"]]
        assert len(rr) == 1 and "해제 가능" in rr[0]["body"], rr
        assert got["state"] == "blocked"  # 자동 재dispatch 없음 — 상태 불변
        log("B2 의존 done → release_ready=True + [release-ready] 댓글 1회 + state=blocked 유지 PASS")
        log("SMOKE-RESULT: PASS (A reconcile-terminate 왕복, B why-blocked 왕복)")
        return 0
    finally:
        for p in procs:
            p.terminate()
        subprocess.run(["/opt/homebrew/bin/tmux", "-L", sock, "kill-server"], capture_output=True)


if __name__ == "__main__":
    sys.exit(main())
