"""0TRW(M568PJK2-0TRW) — TPRQ 격리 환경 실측: 실패 hook 재현.

실서버(uvicorn 스레드)+죽은 훅(127.0.0.1:1)으로 TPRQ 세 가지를 관측한다:
①첫 실패만 댓글 ②지수 backoff(기수락) ③5회 연속 실패 사람 통보 1회.
DELIVERY_RECOVER_S=2로 압축 — 관측 시퀀스 2→4→8→16→32s.
"""
import json
import os
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'server'))
os.environ["TT_NOTIFY_WORKER"] = "1"

import routers.agents as ragents  # noqa: E402
ragents.DELIVERY_RECOVER_S = 2  # 격리 실측용 압축 — 운영 기본값 30s

from app import create_app  # noqa: E402

DB = "/tmp/opencode/0trw.db"
PORT = 18927
if os.path.exists(DB):
    os.remove(DB)

app = create_app(DB)


def run():
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="error")


threading.Thread(target=run, daemon=True).start()
time.sleep(2.5)
BASE = f"http://127.0.0.1:{PORT}"


def req(path, body=None, method=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method or ("POST" if data else "GET"),
                               headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(r))


def step(name, path, body):
    try:
        return req(path, body)
    except urllib.error.HTTPError as e:
        print(f"{name} 실패: {e.code} {e.read().decode()[:200]}")
        raise


step("agents", "/agents", {"name": "deadhook", "base_url": "http://127.0.0.1:1/hook", "receive_mode": "hook"})
card = step("issues", "/issues", {"title": "0TRW 격리 실측", "acceptance": "완료 기준: TPRQ 관측"})
iid = card["id"]
step("claim", f"/issues/{iid}/claim", {"agent": "deadhook"})
did = step("dispatch", f"/issues/{iid}/dispatch",
           {"agent": "deadhook", "message": "0TRQ 실측 발송", "author": "deadhook"})["id"]
print(f"issue={iid} dispatch={did} — 관측 시작 {time.strftime('%H:%M:%S')}")

seen = {}
t0 = time.time()
notice = None
DISPATCH_BODY = {"agent": "deadhook", "message": "0TRQ 실측 발송", "author": "deadhook"}
while time.time() - t0 < 70:
    try:  # 발송자 재시도 루프 — 서버가 backoff 미경과 재전송을 기수락하는지 관측
        req(f"/issues/{iid}/dispatch", DISPATCH_BODY)
    except urllib.error.HTTPError:
        pass
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT fail_count, last_fail_at FROM dispatches WHERE id=?", (did,)).fetchone()
    comments = con.execute("SELECT body, ts FROM comments WHERE issue_id=? AND author='tt-server'",
                           (iid,)).fetchall()
    con.close()
    fc = row["fail_count"]
    if fc and fc not in seen:
        seen[fc] = time.time() - t0
    if fc >= 5 and len(comments) >= 2 and notice is None:
        notice = comments[1]["body"]
    if fc >= 6:  # 6회차까지 관측 — 통보가 정확히 1회인지 확인
        break
    time.sleep(0.3)

final_fc = row["fail_count"]
deltas = [round(seen[k + 1] - seen[k], 1) for k in sorted(seen) if k + 1 in seen]
print(f"fail_count 전이 시각(s): { {k: round(v,1) for k,v in seen.items()} }")
print(f"백오프 간격(기대 2,4,8,16,32): {deltas}")
print(f"tt-server 댓글 {len(comments)}건:")
for c in comments:
    print("  -", c["body"][:90])
print(f"최종 fail_count={final_fc}")

ok = (deltas and all(abs(d - (2 ** (i + 1))) < 0.6 for i, d in enumerate(deltas[:5]))
      and len(comments) == 2 and final_fc >= 5
      and "사람 확인 필요" in (comments[1]["body"] if len(comments) > 1 else ""))
print("RESULT:", "PASS" if ok else "FAIL")

# --- 4항: 성공 전환 후 카운터 초기화 관측 ---
# 훅을 살아있는 로컬 엔드포인트로 교체 → 다음 재전달이 성공 → fail_count 0·last_fail_at NULL
import http.server as _hs

class _OK(_hs.BaseHTTPRequestHandler):
    def do_POST(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, *a):
        pass

ok_srv = threading.Thread(
    target=lambda: _hs.HTTPServer(("127.0.0.1", 18928), _OK).serve_forever(),
    daemon=True)
ok_srv.start()
time.sleep(0.5)
con = sqlite3.connect(DB)
con.execute("UPDATE agents SET base_url='http://127.0.0.1:18928/' WHERE name='deadhook'")
con.commit()
con.close()
reset_t0 = time.time()
row2 = None
while time.time() - reset_t0 < 80:  # fail_count=6 → backoff 64s 경과 대기
    try:  # 재시도 루프 유지 — backoff 경과 후 서버가 성공 전환
        req(f"/issues/{iid}/dispatch", DISPATCH_BODY)
    except urllib.error.HTTPError:
        pass
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    row2 = con.execute("SELECT status, fail_count, last_fail_at FROM dispatches WHERE id=?",
                       (did,)).fetchone()
    con.close()
    if row2["status"] == "ok" and not row2["fail_count"] and not row2["last_fail_at"]:
        break
    time.sleep(0.3)
print(f"성공 전환: status={row2['status']} fail_count={row2['fail_count']} "
      f"last_fail_at={row2['last_fail_at']}")
ok2 = row2["status"] == "ok" and not row2["fail_count"] and not row2["last_fail_at"]
print("RESULT4(초기화):", "PASS" if ok2 else "FAIL")
