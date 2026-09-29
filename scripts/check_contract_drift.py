#!/usr/bin/env python3
"""API-드린처 확인 (통합 카드 t_3b437561, 계약 §2.6 '통합 카드가 API-드린처 재확인').

실 서버 응답(TestClient)의 GET /agents/active, GET /issues/{id}/dispatches 필드를
보드(index.html)가 소비하는 필드명/fixture과 대조 + 오류 행렬 샘플.
불일치 시 nonzero exit. (pytest 통과와 별개로 통합 브랜치에서의 최종 교차 확인용.)

실행: .venv/bin/python scripts/check_contract_drift.py
"""
import json
import pathlib
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

from fastapi.testclient import TestClient  # noqa: E402
from app import create_app  # noqa: E402

ACTIVE_EXPECT = {"dispatch_id", "issue_id", "issue_title", "agent", "machine",
                 "session", "run_state", "started_at", "last_progress_at",
                 "elapsed_s", "last_tail"}
DISPATCH_CONSUMED = {"run_state", "machine", "session", "started_at",
                     "last_progress_at", "last_tail", "ended_at", "status"}
LEGACY_KEYS = {"id", "issue_id", "agent", "author", "message", "context",
               "status", "detail", "ts"}
SECRET = "sekret"


class Hook(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("content-length", 0) or 0))
        self.send_response(200)
        self.end_headers()

    def log_message(self, *a):
        pass


def main():
    import tempfile
    srv = HTTPServer(("127.0.0.1", 0), Hook)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}/hook"

    app = create_app(str(pathlib.Path(tempfile.mkdtemp()) / "tt.db"))
    client = TestClient(app)

    client.post("/agents", json={"name": "drift-bot", "base_url": base, "secret": SECRET})
    iid = client.post("/issues", json={"title": "드리프트 확인", "body": "b"}).json()["id"]
    did = client.post(f"/issues/{iid}/dispatch",
                      json={"agent": "drift-bot", "message": "m"}).json()["id"]

    n_before = len(client.get(f"/issues/{iid}").json()["comments"])  # dispatch 수신 코멘트(기동 관례)

    r = client.post(f"/issues/{iid}/dispatches/{did}/progress",
                    json={"state": "running", "tail": "step-1",
                          "machine": "mac", "session": "tt-drift-bot-1"},
                    headers={"x-tt-dispatch": str(did),
                             "Authorization": f"Bearer {SECRET}"})
    assert r.status_code == 200, r.text
    for i in range(3):  # 반복 진행 투영도 코멘트 무생성
        client.post(f"/issues/{iid}/dispatches/{did}/progress",
                    json={"state": "running", "tail": f"step {i}"},
                    headers={"x-tt-dispatch": str(did),
                             "Authorization": f"Bearer {SECRET}"})

    errs = []

    # 1) GET /agents/active 필드 = 보드 fixture/allowlist
    act = client.get("/agents/active").json()
    assert isinstance(act, list) and len(act) == 1, act
    missing = ACTIVE_EXPECT - set(act[0].keys())
    if missing:
        errs.append(f"/agents/active 필드 누락: {sorted(missing)}")
    extra = set(act[0].keys()) - ACTIVE_EXPECT
    if extra:
        errs.append(f"/agents/active 추가 필드(보드 allowlist 밖): {sorted(extra)}")
    if act[0]["run_state"] != "running":
        errs.append(f"run_state 불일치: {act[0]['run_state']}")

    # 2) GET /issues/{id}/dispatches — 신규 필드 병기 + 기존 키 보존(하위 호환)
    ds = client.get(f"/issues/{iid}/dispatches").json()
    row = next(d for d in ds if d["id"] == did)
    for k in DISPATCH_CONSUMED | LEGACY_KEYS:
        if k not in row:
            errs.append(f"dispatches 행 필드 누락: {k}")
    if row.get("run_state") != "running" or row.get("last_tail") != "step 2":  # last-write-wins
        errs.append(f"투영 값 불일치: run_state={row.get('run_state')} tail={row.get('last_tail')}")

    # 코멘트 무생성 (진행 투영은 dispatch 레코드만; dispatch 수신 코멘트 대비 증가 0)
    com = client.get(f"/issues/{iid}").json()["comments"]
    if len(com) != n_before:
        errs.append(f"진행 투영이 코멘트를 생성: +{len(com)-n_before}건")

    # 3) 보드 fixture 파일이 실 응답과 동일 필드집인지
    fx = json.loads((ROOT / "tests" / "fixtures" / "board_active.json").read_text())
    fixture_keys = set(fx[0].keys())
    if fixture_keys != ACTIVE_EXPECT:
        diff = (ACTIVE_EXPECT - fixture_keys) | {k for k in fixture_keys if k not in ACTIVE_EXPECT}
        errs.append(f"fixture↔API 필드 차이: {sorted(diff)}")

    # 4) 오류 행렬 샘플 (계약 §2.2)
    hdr = lambda d, s: {"x-tt-dispatch": str(d), "Authorization": f"Bearer {s}"}
    if client.post(f"/issues/{iid}/dispatches/999999/progress", json={"state": "running"},
                   headers=hdr(999999, SECRET)).status_code != 404:
        errs.append("미존재 did 404 아님")
    if client.post(f"/issues/{iid}/dispatches/{did}/progress", json={"state": "running"},
                   headers=hdr(did, "wrong")).status_code != 403:
        errs.append("secret 불일치 403 아님")
    if client.post(f"/issues/{iid}/dispatches/{did}/progress", json={"state": "bogus"},
                   headers=hdr(did, SECRET)).status_code != 422:
        errs.append("비enum state 422 아님")

    srv.shutdown()
    if errs:
        print("DRIFT-FAIL")
        for e in errs:
            print(" -", e)
        sys.exit(1)
    print("DRIFT-OK: active 11필드 = fixture = 보드 allowlist; dispatches 신규7+기존9 보존; "
          "코멘트 0; 404/403/422 행렬 정상")


if __name__ == "__main__":
    main()
