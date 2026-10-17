#!/usr/bin/env python3
"""K4Q7(M53XP2ZA-K4Q7) — 에이전트별 webhook secret 교체.

2026-10-17 점검: 10개 에이전트 중 7개(codex, opencode, agy, claude, hermes-m2max,
cursor, cursor-review)가 같은 secret을 공유 + GET /agents 원문 노출로 앞 3글자까지
점검 도구에 찍힘. 읽기 경로 차단은 서버 코드(마스킹)로, 교체는 이 스크립트로.

동작: TT API에서 에이전트 목록을 읽고, secret을 쓰는 대상마다 새 값을 생성해 PATCH.
새 값은 stdout에 절대 찍지 않는다 — --out 파일(0600)에만 기록.

사용: python3 rotate_agent_secrets.py --base URL --out ~/agent-secrets.json [--only a,b] [--dry-run]

주의: 교체 즉시 해당 에이전트의 러너/리스너 설정(같은 값)을 갱신해야 /progress·
웹훅이 계속 동작한다. 러너는 시동 시 secret을 읽으므로 러너 재시작과 같은 창에서
실행할 것(10/24 배포 게이트 일괄 작업에 묶여 있다).
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import stat
import urllib.request

RUNNER_SIDED = ("opencode", "codex", "agy", "claude", "cursor", "cursor-review",
                "hermes-m2max")  # 시동 시 secret 파일을 읽는 측 — 재시작 필요


def _req(base: str, path: str, method: str = "GET", body=None, timeout: int = 30):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(base.rstrip("/") + path, data=data, method=method)
    if data:
        r.add_header("content-type", "application/json")
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return json.load(resp)


def main() -> int:
    p = argparse.ArgumentParser(description="에이전트별 webhook secret 교체 (K4Q7)")
    p.add_argument("--base", default="http://127.0.0.1:7800")
    p.add_argument("--out", default="~/agent-secrets.json")
    p.add_argument("--only", default="", help="쉼표 구분 에이전트명 한정")
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args()
    agents = _req(a.base, "/agents")
    only = {s.strip() for s in a.only.split(",") if s.strip()}
    out_path = os.path.expanduser(a.out)
    existing = {}
    if os.path.exists(out_path):
        with open(out_path) as f:
            existing = json.load(f)
    results = {}
    for ag in agents:
        name = ag["name"]
        if only and name not in only:
            continue
        if not ag.get("base_url"):
            continue
        new = secrets.token_urlsafe(32)
        results[name] = new
        if a.dry_run:
            print(f"[dry-run] {name}: 교체 예정")
            continue
        _req(a.base, f"/agents/{name}", "PATCH", {"secret": new})
        print(f"PATCHed {name} — 러너/리스너 설정 갱신 필요: {'예(재시작)' if name in RUNNER_SIDED else '즉시 유효'}")
    if a.dry_run or not results:
        return 0
    existing.update(results)
    fd = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(existing, f, ensure_ascii=False, indent=1, sort_keys=True)
    os.chmod(out_path, stat.S_IRUSR | stat.S_IWUSR)
    print(f"새 값 기록: {out_path} (0600) — 각 머신 설정에 반영 후 이 파일에서 삭제 권장")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
