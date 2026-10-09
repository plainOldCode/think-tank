#!/usr/bin/env python3
"""TT 지표 수집기 초안 — TT 개선#3e (M4FFCB9J-2AAB) 2주 실험 측정 창구.

계약 기준(카드 본문): 착수시간·리뷰통과율·재작업률·stall복구율·개입횟수 5종.
카드 생성 수는 지표에서 제외한다.

데이터 소스 (TT 서버 API, stdlib-only):
- GET /issues?limit=N            : 카드 스냅샷 — created_at/started_at/completed_at/
                                   verified_at/execution_attempt/todo_since/heartbeat_at/
                                   blocked_notified_at/lease_expires
- GET /events?after_seq=&limit=  : 이벤트 로그 — issue.created/issue.updated/
                                   comment.added/dispatch.created/dispatch.updated/message.posted
- GET /issues/{id}/dispatches    : 디스패치 이력 — run_state/last_progress_at/attempt

측정 규약:
- 착수시간   = started_at - created_at (started_at 존재 카드 대상, p50/p90).
               이벤트 로그 시작(2026-10-09) 이전 이력은 스냅샷 한계로 최근 수령 기준만 계산.
- 리뷰통과율 = completed_at≠null && verified_at≠null 카드 / completed_at≠null 카드.
               verify 지연 = verified_at - completed_at의 p50.
- 재작업률   = execution_attempt≥2 && started_at≠null 카드 / started_at≠null 카드.
               이벤트로 review→todo 반납(재작업 위임) 횟수도 세어 병기.
- stall      = state가 in_progress/review인데 (lease 만료 또는 heartbeat 60분 경과) 카드.
               복구 신호 = 이후 issue.updated에서 heartbeat_at 갱신 또는 상태 전진.
- 개입횟수   = 사람 액터(skshim@*)의 comment.added 중 dispatch 지시가 아닌 것
               + blocked 전이 + 사람 message.posted. v1은 원시 카운트만 산출(실험 기간에 정밀화).

사용: python3 metrics_collect.py [--base URL] [--json] [--out FILE]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta

STALL_HEARTBEAT_MIN = 60  # 이 시간 이상 heartbeat 정지 카드를 stall로 본다(초안 기본값)
HUMAN_ACTORS = ("skshim",)  # 사람 식별 접두 — 실험 기간에 확정


def get(base: str, path: str, timeout: int = 30):
    with urllib.request.urlopen(base.rstrip("/") + path, timeout=timeout) as r:
        return json.loads(r.read().decode())


def parse_ts(s: str | None):
    if not s:
        return None
    return datetime.fromisoformat(s)


def fetch_events(base: str):
    events, after = [], 0
    while True:
        batch = get(base, f"/events?after_seq={after}&limit=500")
        evs = batch.get("events", [])
        events.extend(evs)
        if len(evs) < 500:
            return events
        after = evs[-1]["seq"]


def pctl(xs, q):
    if not xs:
        return None
    xs = sorted(xs)
    i = min(len(xs) - 1, max(0, round(q * (len(xs) - 1))))
    return xs[i]


def fmt_min(m):
    if m is None:
        return "-"
    if m < 120:
        return f"{m:.0f}분"
    return f"{m/60:.1f}시간"


def collect(base: str):
    now = datetime.now().astimezone()
    issues = get(base, "/issues?limit=1000")
    if isinstance(issues, dict):
        issues = issues.get("issues", [])
    events = fetch_events(base)

    # ── 착수시간 ──────────────────────────────────────────────
    t2s = []
    for it in issues:
        st, cr = parse_ts(it.get("started_at")), parse_ts(it.get("created_at"))
        if st and cr:
            t2s.append((st - cr).total_seconds() / 60.0)

    # ── 리뷰통과율 ────────────────────────────────────────────
    completed = [it for it in issues if it.get("completed_at")]
    passed = [it for it in completed if it.get("verified_at")]
    verify_lat = []
    for it in passed:
        va, ca = parse_ts(it.get("verified_at")), parse_ts(it.get("completed_at"))
        if va and ca:
            verify_lat.append((va - ca).total_seconds() / 60.0)
    pass_rate = (len(passed) / len(completed) * 100.0) if completed else None

    # ── 재작업률 ──────────────────────────────────────────────
    started = [it for it in issues if it.get("started_at")]
    reworked = [it for it in started if (it.get("execution_attempt") or 1) >= 2]
    rework_rate = (len(reworked) / len(started) * 100.0) if started else None
    rework_returns = 0  # review→todo 반납(재작업 위임)
    for e in events:
        if e.get("kind") == "issue.updated":
            pl = {}
            try:
                pl = json.loads(e.get("payload") or "{}")
            except json.JSONDecodeError:
                pass
            if pl.get("state") == "todo" and "state" in pl.get("fields", []):
                rework_returns += 1

    # ── stall 현황 ────────────────────────────────────────────
    active = [it for it in issues if it.get("state") in ("in_progress", "review")]
    stalled = []
    for it in active:
        hb = parse_ts(it.get("heartbeat_at"))
        lease = parse_ts(it.get("lease_expires"))
        stale_hb = hb is None or (now - hb) > timedelta(minutes=STALL_HEARTBEAT_MIN)
        lease_expired = lease is not None and lease < now
        if stale_hb or lease_expired:
            stalled.append((it["id"], it.get("state"), it.get("lease_by") or "-"))

    # ── 개입 원시 카운트 ─────────────────────────────────────
    human_comments, blocked_events, human_messages = 0, 0, 0
    for e in events:
        k = e.get("kind")
        if k == "comment.added":
            pl = {}
            try:
                pl = json.loads(e.get("payload") or "{}")
            except json.JSONDecodeError:
                pass
            if any(pl.get("author", "").startswith(h) for h in HUMAN_ACTORS):
                human_comments += 1
        elif k == "message.posted":
            pl = {}
            try:
                pl = json.loads(e.get("payload") or "{}")
            except json.JSONDecodeError:
                pass
            if any(pl.get("author", "").startswith(h) for h in HUMAN_ACTORS):
                human_messages += 1
        elif k == "issue.updated":
            pl = {}
            try:
                pl = json.loads(e.get("payload") or "{}")
            except json.JSONDecodeError:
                pass
            if pl.get("state") == "blocked":
                blocked_events += 1

    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "window_note": f"events seq 1~{events[-1]['seq'] if events else 0} (로그 시작일 이후)",
        "issues_scanned": len(issues),
        "events_scanned": len(events),
        "time_to_start_min": {"p50": pctl(t2s, 0.5), "p90": pctl(t2s, 0.9), "n": len(t2s)},
        "review_pass_rate_pct": pass_rate,
        "review_pass": {"passed": len(passed), "submitted": len(completed)},
        "verify_latency_min": {"p50": pctl(verify_lat, 0.5), "n": len(verify_lat)},
        "rework_rate_pct": rework_rate,
        "rework": {"reworked": len(reworked), "started": len(started), "review_to_todo_returns": rework_returns},
        "stall": {"threshold_min": STALL_HEARTBEAT_MIN, "active": len(active), "stalled": stalled},
        "intervention": {"human_comments": human_comments, "human_messages": human_messages, "blocked_transitions": blocked_events},
    }


def dashboard(m: dict) -> str:
    t2s, rp, rw, stl, iv = m["time_to_start_min"], m["review_pass"], m["rework"], m["stall"], m["intervention"]
    lines = [
        "## TT 지표 대시보드 (초안)",
        f"- 생성: {m['generated_at']} | 스캔: 카드 {m['issues_scanned']} / 이벤트 {m['events_scanned']}",
        "",
        "| 지표 | 값 | 근거 수 |",
        "|---|---|---|",
        f"| 착수시간 p50 | {fmt_min(t2s['p50'])} | n={t2s['n']} |",
        f"| 착수시간 p90 | {fmt_min(t2s['p90'])} | n={t2s['n']} |",
        f"| 리뷰통과율 | {('%.1f%%' % m['review_pass_rate_pct']) if m['review_pass_rate_pct'] is not None else '-'} | {rp['passed']}/{rp['submitted']} |",
        f"| verify 지연 p50 | {fmt_min(m['verify_latency_min']['p50'])} | n={m['verify_latency_min']['n']} |",
        f"| 재작업률 | {('%.1f%%' % m['rework_rate_pct']) if m['rework_rate_pct'] is not None else '-'} | {rw['reworked']}/{rw['started']} |",
        f"| review→todo 반납(재작업 위임) | {rw['review_to_todo_returns']}회 | events |",
        f"| stall 카드(기준 {stl['threshold_min']}분) | {len(stl['stalled'])}/{stl['active']} active | snapshot |",
        f"| 개입: 사람 코멘트/메시지/blocked | {iv['human_comments']}/{iv['human_messages']}/{iv['blocked_transitions']} | events |",
        "",
    ]
    if stl["stalled"]:
        lines.append("stall 상세: " + "; ".join(f"{i}({s}, lease {a})" for i, s, a in stl["stalled"][:5]))
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.environ.get("TT_URL", "http://localhost:7800"), help="TT 서버 URL — 운영 주소는 환경변수 TT_URL 또는 이 플래그로 전달(레포에 기록하지 않는다)")
    ap.add_argument("--json", action="store_true", help="JSON만 출력")
    ap.add_argument("--out", help="결과 파일 저장 경로")
    args = ap.parse_args()

    m = collect(args.base)
    text = json.dumps(m, ensure_ascii=False, indent=2) if args.json else dashboard(m)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    print(text)


if __name__ == "__main__":
    sys.exit(main())
