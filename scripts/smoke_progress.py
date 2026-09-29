#!/usr/bin/env python3
"""E2E 스모크 (TT M3EREF97-FXWQ, dispatch#57): 진행 투영·활성 조회 실 프로세스 실증.

임시 DB + 격리 tmux socket + 실제 dispatch 왕복:
  dispatch 수신 → run_state=queued → 세션 기동 후 running(last_tail 갱신)
  → 주기 progress 갱신(last_progress_at 전진) → 임계 축약 무음으로 stalled 투영
  → 종료 시 finished. 코멘트는 시작/STALL/종료만(진행 코멘트 0).

한계: 실제 agent CLI(codex 등) 대신 echo/sleep 목 드라이버 — 투영·조회 계약 검증 목적.
stall 임계는 env로 축약(TT_STALL_SILENCE_S/TT_STALL_KILL_AFTER_S).

실행: .venv/bin/python scripts/smoke_progress.py  (repo root 기준; smoke_5722와 동일 격리)
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
SERVER_PORT = int(os.environ.get("SMOKE_PORT", "7812"))
RUNNER_PORT = int(os.environ.get("SMOKE_RUNNER_PORT", "7798"))
TT = f"http://127.0.0.1:{SERVER_PORT}"
SECRET = "smoke-progress-secret"


def http(method, path, payload=None, base=TT, timeout=15, headers=None):
    data = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    hdrs = {"content-type": "application/json"}
    hdrs.update(headers or {})
    req = urllib.request.Request(base + path, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            return r.status, (json.loads(body) if body else None)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")
    except Exception as e:
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
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="smokeprog."))
    (tmp / "state").mkdir()
    (tmp / "runs").mkdir()
    (tmp / "inbox").mkdir()
    (tmp / "secret").write_text(SECRET)
    sock = "smokeprog"
    (tmp / "bin").mkdir()
    shim = tmp / "bin" / "tmux"
    shim.write_text("#!/bin/sh\nexec /opt/homebrew/bin/tmux -L %s \"$@\"\n" % sock)
    shim.chmod(0o755)

    def tmux_has(name):
        r = subprocess.run(["/opt/homebrew/bin/tmux", "-L", sock, "has-session", "-t", name],
                           capture_output=True)
        return r.returncode == 0

    # 목 드라이버: 3초간 2초마다 출력 후 exit 0. stall 축약(5s/5s)과 조합해
    # running(출력 진행) → (drv sleep 구간) 무음 stalled 경로까지 한 세션에서 본다.
    drv = tmp / "driver.sh"
    drv.write_text("#!/bin/sh\n"
                   "for i in 1 2 3; do echo \"tick-$i\"; sleep 2; done\n"
                   "sleep 25\n"
                   "echo \"all-done\"\n")
    drv.chmod(0o755)
    (tmp / "agents.json").write_text(json.dumps({"machine": "smokebox", "agents": {
        "tick": {"driver": "plain", "binary": str(drv), "workspace": str(tmp)}}}))
    env_r = dict(os.environ,
                 TT_RUNNER_STATE=str(tmp / "state"),
                 TT_RUNNER_SECRET=str(tmp / "secret"),
                 TT_RUNNER_REGISTRY=str(tmp / "agents.json"),
                 TT_RUNNER_BIND="127.0.0.1", TT_RUNNER_PORT=str(RUNNER_PORT),
                 TT_RUNNER_PROGRESS_S="2",
                 TT_STALL_SILENCE_S="6", TT_STALL_KILL_AFTER_S="600",
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

        st, _ = http("POST", "/agents", {"name": "tick", "secret": SECRET,
                                         "base_url": f"http://127.0.0.1:{RUNNER_PORT}/hook",
                                         "release_hook": True})
        assert st == 201
        st, issue = http("POST", "/issues", {"title": "E2E-progress 진행 투영 왕복"})
        iid = issue["id"]
        st, d = http("POST", f"/issues/{iid}/dispatch",
                     {"agent": "tick", "message": "3틱 출력 후 잠들기(실증용)", "author": "smoke"})
        assert st == 201 and d["status"] == "ok", d
        did = d["id"]
        sess = "tt-tick-%s" % did

        def disp():
            _, rows = http("GET", f"/issues/{iid}/dispatches")
            return next(x for x in rows if x["id"] == did)

        # 1. queued/running 전이 관측 (dispatch#57 요구: 시작이 보인다)
        wait(lambda: disp()["run_state"] in ("queued", "running"), 20,
             "run_state queued/running")
        log("P1 수신 후 run_state=%s machine=%s session=%s" %
            (disp()["run_state"], disp()["machine"], disp()["session"]))
        # 2. 세션 실기동 + running
        wait(lambda: tmux_has(sess) and disp()["run_state"] == "running", 20,
             "tmux 실행 + running")
        log("P2 tmux %s 실행 + run_state=running" % sess)
        # 3. 주기 progress: last_progress_at 전진 + last_tail에 드라이버 출력
        t0 = disp()["last_progress_at"]
        wait(lambda: disp()["last_progress_at"] > t0, 25, "last_progress_at 전진")
        # 드라이버 첫 tick은 1초 후 — 출력 반영까지 대기 (처음 1발은 빈 꼬리 가능)
        wait(lambda: "tick-" in disp()["last_tail"], 25, "last_tail에 tick 출력")
        log("P3 progress 갱신: %s → %s tail=%r" % (t0, disp()["last_progress_at"],
                                                   disp()["last_tail"]))
        # 4. 활성 조회: 이 dispatch만 queued/running/stalled로 노출
        _, act = http("GET", "/agents/active")
        me = [a for a in act if a["dispatch_id"] == did]
        assert len(me) == 1 and me[0]["run_state"] == "running", act
        assert isinstance(me[0]["elapsed_s"], int) and me[0]["issue_title"], me[0]
        log("P4 GET /agents/active: %d건 중 target run_state=%s elapsed_s=%s" %
            (len(act), me[0]["run_state"], me[0]["elapsed_s"]))
        # 5. tick 출력 종료 후 무음 6s → stalled (임계 축약; 판정 소스는 러너)
        #    세션 꼬리가 'sleep' 상태로 고착 — 파일 mtime도 정지
        wait(lambda: disp()["run_state"] == "stalled", 60, "stalled 전이")
        _, act2 = http("GET", "/agents/active")
        me2 = [a for a in act2 if a["dispatch_id"] == did]
        assert me2 and me2[0]["run_state"] == "stalled", me2
        log("P5 무음 임계 초과 → run_state=stalled (활성 목록에 유지, 서버 재계산 없음)")
        # 6. 수동 종료(스모크 시간 절약): 세션 kill → 러너 finalize → finished/failed
        subprocess.run(["/opt/homebrew/bin/tmux", "-L", sock, "kill-session", "-t", sess],
                       capture_output=True)
        wait(lambda: disp()["run_state"] in ("finished", "failed"), 30, "종료 투영")
        _, act3 = http("GET", "/agents/active")
        assert not [a for a in act3 if a["dispatch_id"] == did], "종료 후 활성 잔존"
        log("P6 종료 후 run_state=%s + 활성 목록 소멸" % disp()["run_state"])
        # 7. 코멘트 회귀: 진행 코멘트 0 — dispatch message/시작 코멘트만
        _, got = http("GET", f"/issues/{iid}")
        bodies = [c["body"] for c in got["comments"]]
        prog = [b for b in bodies if b.startswith("progress")]
        assert not prog, prog
        assert any(b.startswith("runner:") for b in bodies)
        log("P7 코멘트 %d건(시작/종료만) — 진행 코멘트 0" % len(bodies))
        log("SMOKE-RESULT: PASS (queued→running→progress→active→stalled→종료 소멸)")
        return 0
    finally:
        for p in procs:
            p.terminate()
        subprocess.run(["/opt/homebrew/bin/tmux", "-L", sock, "kill-server"], capture_output=True)


if __name__ == "__main__":
    sys.exit(main())
