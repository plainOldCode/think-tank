import pytest
from fastapi.testclient import TestClient

from app import create_app


@pytest.fixture
def client(tmp_path):
    app = create_app(str(tmp_path / "tt.db"))
    return TestClient(app)


def mk(client, **kw):
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
