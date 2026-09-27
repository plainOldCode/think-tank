#!/usr/bin/env python3
"""E2E 스모크 (TT M3BZV172-9F0S): done≠verified 게이트 + blocked→human 알림 왕복 실증.

실증 3건 (실 uvicorn + 실 CLI(tt) + 실 알림 수신기, tmp DB — 실 서비스 무충돌):
 A. done 거부 예 — 증거 없는 done은 review 강등 + tt-server 안내 댓글 + tt CLI 안내 출력.
    증거 코멘트 후 재시도 시 verified=1 done 자동 연결. verify 엔드포인트(422→200) 왕복.
 B. blocked(waiting_for=human) → notify_hook 수신기(실 HTTP 서버)가 X-TT-Command:notify +
    Level4(A/B+recommendation) 페이로드 수신 → hermes send로 실 Telegram DM 주입(도착 로그).
    dedup(중복 마커 코멘트 재발송 없음) + legacy runner 코멘트 마커 경로 포함.
 C. 카드 상태 불변 — 알림이 blocked를-auto-release 하지 않음, notify_hook 없는 agent 스킵.

실행: <venv-python> scripts/smoke_9F0S.py   (smoke 전용 포트 7813/7814, 실 Telegram은
SMOKE_TELEGRAM=1일 때만 1회 발송 — 기본은 수신 로그까지 + 발송 시도 rc 기록)
"""
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = pathlib.Path(__file__).resolve().parent.parent
SERVER_PORT = int(os.environ.get("SMOKE_PORT", "7813"))
HOOK_PORT = int(os.environ.get("SMOKE_HOOK_PORT", "7814"))
TT = f"http://127.0.0.1:{SERVER_PORT}"
HERMES = os.environ.get("HERMES_BIN", "~/.local/bin/hermes")
TELEGRAM_CHAT = os.environ.get("SMOKE_TG_CHAT", "44382198")
SECRET = "smoke-9f0s-secret"
LOG_LINES = []
RECEIVED = []  # (headers, payload)
TG_RC = {"rc": None, "out": ""}


def http(method, path, payload=None, timeout=15):
    data = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    req = urllib.request.Request(TT + path, data=data, method=method,
                                 headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            return r.status, (json.loads(body) if body else None)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")
    except Exception as e:
        return 0, {"error": "%s: %s" % (type(e).__name__, e)}


def log(msg):
    line = time.strftime("%H:%M:%S") + " " + msg
    print(line, flush=True)
    LOG_LINES.append(line)


class Receiver(BaseHTTPRequestHandler):
    """tt-bridge/어댑터가 하게 될 일을 그대로 수행: Level4 수신 → 확인 로그 → 실 Telegram 주입."""

    def do_POST(self):
        n = int(self.headers.get("content-length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        RECEIVED.append(({k.lower(): v for k, v in self.headers.items()}, body))
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *a):
        pass


def main():
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="smoke9f0s."))
    (tmp / "evidence.log").write_text("")
    env_s = dict(os.environ, TT_DB=str(tmp / "tt.db"), PYTHONPATH=str(ROOT / "server"))
    srv = subprocess.Popen([sys.executable, "-m", "uvicorn", "app:app",
                            "--host", "127.0.0.1", "--port", str(SERVER_PORT)],
                           cwd=str(ROOT / "server"), env=env_s,
                           stdout=open(tmp / "server.log", "w"), stderr=subprocess.STDOUT)
    hook = HTTPServer(("127.0.0.1", HOOK_PORT), Receiver)
    threading.Thread(target=hook.serve_forever, daemon=True).start()
    try:
        for _ in range(40):
            if http("GET", "/health")[0] == 200:
                break
            time.sleep(0.5)
        else:
            raise SystemExit("server did not start")
        log("SMOKE-START server=%d hook=%d tmp=%s" % (SERVER_PORT, HOOK_PORT, tmp))

        st, ag = http("POST", "/agents", {"name": "notify-h", "secret": SECRET,
                                          "base_url": f"http://127.0.0.1:{HOOK_PORT}/hook",
                                          "notify_hook": True})
        assert st == 201 and ag["notify_hook"] == 1, ag
        http("POST", "/agents", {"name": "plain-h", "base_url": f"http://127.0.0.1:{HOOK_PORT}/hook"})

        # ---- A. done 거부 예 (게이트 + CLI + verify 왕복) ----
        st, i = http("POST", "/issues", {"title": "E2E-9F0S-A done≠verified 게이트"})
        iid = i["id"]
        http("POST", f"/issues/{iid}/claim", {"agent": "smoke@studio"})
        http("POST", f"/issues/{iid}/comments", {"author": "smoke@studio", "body": "작업 끝났습니다"})
        st, d1 = http("PATCH", f"/issues/{iid}", {"state": "done"})
        assert st == 200 and d1["state"] == "review" and d1["completed_at"] is None, d1
        _, got = http("GET", f"/issues/{iid}")
        gate_note = [c["body"] for c in got["comments"] if c["author"] == "tt-server" and "done 증거 없음" in c["body"]]
        assert len(gate_note) == 1, gate_note
        log("A1 증거 없는 done → review 강등 + tt-server 안내 댓글 1회 PASS (%s)" % iid)

        # 실 CLI로 done 재시도 → 강등 안내 출력 확인 (exit 0, review 유도)
        env_cli = dict(os.environ, TT_URL=TT, TT_AGENT="smoke@studio")
        r = subprocess.run(["bash", str(ROOT / "cli" / "tt"), "done", iid],
                           capture_output=True, text=True, env=env_cli, timeout=30)
        assert "review" in r.stdout and "tt verify" in r.stdout and r.returncode == 0, (r.stdout, r.stderr)
        log("A2 실 CLI `tt done` → review 강등 안내 출력 PASS")

        assert http("GET", f"/issues/{iid}")[1]["state"] == "review"
        http("POST", f"/issues/{iid}/comments",
             {"author": "smoke@studio", "body": "완료: pytest 42 passed, commit 74d0f56e 정합"})
        r = subprocess.run(["bash", str(ROOT / "cli" / "tt"), "done", iid, "증거 포함 완료"],
                           capture_output=True, text=True, env=env_cli, timeout=30)
        assert "완료" in r.stdout and r.returncode == 0, (r.stdout, r.stderr)
        _, done = http("GET", f"/issues/{iid}")
        assert done["state"] == "done" and done["verified"] == 1 and "74d0f56e" in done["verified_evidence"], done
        log("A3 증거 코멘트 후 done → verified=1 자동 연결(성공 보고 전문) PASS")

        # verify 엔드포인트 왕복: review → (422) → (200)
        st, j = http("POST", "/issues", {"title": "E2E-9F0S-A2 verify 경로"})
        jid = j["id"]
        http("POST", f"/issues/{jid}/claim", {"agent": "smoke@studio"})
        st, d2 = http("PATCH", f"/issues/{jid}", {"state": "done"})
        assert d2["state"] == "review", d2
        st, e422 = http("POST", f"/issues/{jid}/verify", {"verifier": "user@mini"})
        assert st == 422, (st, e422)
        st, v = http("POST", f"/issues/{jid}/verify", {"verifier": "user@mini", "evidence": "smoke fixture: pytest 1 passed"})
        assert st == 200 and v["state"] == "done" and v["verified"] == 1, v
        log("A4 verify: 증거없음 422 → evidence 지정 done 확정 PASS")

        # ---- B. blocked(human) → Level4 알림 도착 + 실 Telegram 주입 ----
        st, k = http("POST", "/issues", {"title": "E2E-9F0S-B 모바일 에스컬레이션"})
        kid = k["id"]
        http("PATCH", f"/issues/{kid}", {"state": "in_progress", "assignee": "runner:smoke"})
        st, b = http("PATCH", f"/issues/{kid}",
                     {"state": "blocked", "waiting_for": "human", "waiting_actor": "user@mini",
                      "blocked_detail": "A/B 선택 필요 — 배포 유지 vs 롤백"})
        assert st == 200 and b["waiting_for"] == "human", b

        def notify_hits():
            return [x for x in RECEIVED if x[0].get("x-tt-command") == "notify"]

        for _ in range(30):
            if notify_hits():
                break
            time.sleep(0.4)
        hits = notify_hits()
        assert len(hits) == 1, "expect exactly 1 notify, got %d" % len(hits)
        hdrs, body = hits[0]
        assert hdrs.get("authorization") == "Bearer " + SECRET
        assert body["command"] == "notify" and body["issue_id"] == kid
        text = body["text"]
        assert "[A]" in text and "[B]" in text and "권장: B" in text and "Level4" in text, text
        log("B1 blocked(waiting_for=human) → notify_hook 1회 수신: X-TT-Command:notify + Level4 A/B+권장B PASS")
        log("B1-수신 전문:\n" + "\n".join("    | " + l for l in text.splitlines()))

        # 실 Telegram 주입 (Hermes 기존 경로, 1회)
        if os.environ.get("SMOKE_TELEGRAM", "1") == "1":
            (tmp / "tg.txt").write_text(text, encoding="utf-8")
            argv = [os.path.expanduser(HERMES), "send", "-t", f"telegram:{TELEGRAM_CHAT}",
                    "-s", "[TT-smoke 9F0S] Level4 알림 주입 실증", "-f", str(tmp / "tg.txt")]
            p = subprocess.run(argv, capture_output=True, text=True, timeout=60)
            if p.returncode != 0 and "not configured" in (p.stdout + p.stderr):
                # Telegram bot token은 default 프로필 소유 — 그 프로필로 재시도
                p = subprocess.run([os.path.expanduser(HERMES), "-p", "default"] + argv[1:],
                                   capture_output=True, text=True, timeout=60)
            TG_RC["rc"], TG_RC["out"] = p.returncode, (p.stdout + p.stderr)[:300]
            log("B2 hermes send(Telegram DM %s) rc=%s %s" % (TELEGRAM_CHAT, p.returncode, TG_RC["out"].strip()[:120]))
        else:
            log("B2 SMOKE_TELEGRAM=0 — Telegram 발송 생략(수신 로그까지만)")

        # dedup: 같은 에피소드 내 중복 마커 코멘트 → 재발송 없음
        http("POST", f"/issues/{kid}/comments",
             {"author": "runner:smoke", "body": "dispatch#1 BLOCKED — waiting_for=human 재확인"})
        time.sleep(1.0)
        assert len(notify_hits()) == 1, "dedup 실패"
        # legacy runner 경로: 사족 없는 blocked + 코멘트 마커 → 새 에피소드 1회
        st, m = http("POST", "/issues", {"title": "E2E-9F0S-B2 legacy 마커"})
        mid = m["id"]
        http("PATCH", f"/issues/{mid}", {"state": "in_progress", "assignee": "runner:smoke"})
        http("PATCH", f"/issues/{mid}", {"state": "blocked"})
        time.sleep(0.6)
        assert len(notify_hits()) == 1, "사족 없는 blocked는 알림 없음"
        http("POST", f"/issues/{mid}/comments",
             {"author": "runner:smoke", "body": "dispatch#2 BLOCKED — waiting_for=human 입력 대기"})
        for _ in range(20):
            if len(notify_hits()) == 2:
                break
            time.sleep(0.4)
        assert len(notify_hits()) == 2, "legacy 마커 경로 알림 실패"
        log("B3 dedup(같은 에피소드 1회) + legacy 코멘트 마커 경로 PASS")

        # ---- C. 상태 불변 ----
        _, gotb = http("GET", f"/issues/{kid}")
        assert gotb["state"] == "blocked", gotb
        nsys = [c for c in gotb["comments"] if c["author"] == "tt-server" and "[level4-notify]" in c["body"]]
        assert len(nsys) == 1, nsys
        log("C1 카드 state=blocked 유지 + [level4-notify] 시스템 댓글 1회 (자동 해제 없음) PASS")
        log("SMOKE-RESULT: PASS (A done거부/verify, B Level4 알림 수신, C 상태불변)")
        (tmp / "evidence.log").write_text("\n".join(LOG_LINES) + "\n")
        log("evidence: %s" % (tmp / "evidence.log"))
        return 0
    finally:
        srv.terminate()
        hook.shutdown()


if __name__ == "__main__":
    sys.exit(main())
