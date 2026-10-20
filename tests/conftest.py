import os
import tempfile

import pytest
from fastapi.testclient import TestClient

from app import create_app

# §8.1 판단 로그 격리 — JudgeLog 기본 경로(judge-log.jsonl)가 리포 루트에 생기지
# 않게 한다(커밋 전 rm 습관의 근본 해결). 명시적 경로를 쓰는 테스트는 무관.
os.environ.setdefault("TT_JUDGE_LOG", os.path.join(
    tempfile.gettempdir(), "tt-test-judge-%d.jsonl" % os.getpid()))


@pytest.fixture
def client(tmp_path):
    # 리뷰 R5: TestClient를 컨텍스트로 사용 — 앱 종료(shutdown)까지 실행해
    # 워커 stop+등록 해제가 매 테스트에서 일어난다 (스레드·연결 누적 방지).
    app = create_app(str(tmp_path / "tt.db"))
    with TestClient(app) as c:
        yield c


def human_done(client, iid, note="테스트 승인"):
    r = client.post(f"/issues/{iid}/verify", json={"verifier": "tester", "human": True, "evidence": note})
    assert r.status_code == 200, r.text
    return r


def mk(client, **kw):
    # TT 개선#3a 게이트 통과 기본값 — acceptance 필요 테스트는 kw로 덮어씀
    kw.setdefault("acceptance", "완료 기준: 전체 회귀 통과")
    return client.post("/issues", json={"title": "t", **kw}).json()


import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer


class Hook(BaseHTTPRequestHandler):
    received = []
    mode = "ok"

    def do_POST(self):
        n = int(self.headers.get("content-length", 0))
        Hook.received.append(({k.lower(): v for k, v in self.headers.items()}, json.loads(self.rfile.read(n))))
        if Hook.mode == "slow":
            time.sleep(2.0)  # 느린 수신기 — 락 점유 회귀 경계용 (리뷰 R4a)
        if Hook.mode == "fail":
            self.send_response(500)
            self.end_headers()
            return
        payload = json.dumps({"context": "ses_X1"}).encode() if Hook.mode == "ctx" else b"{}"
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture
def hook_server():
    Hook.received, Hook.mode = [], "ok"
    srv = HTTPServer(("127.0.0.1", 0), Hook)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/hook"
    srv.shutdown()
