#!/usr/bin/env python3
"""TT 지표 수집기 v2 — TT 개선#3e (M4FFCB9J-2AAB) 2주 실험 측정 창구.

v2는 독립 리뷰(TT 코멘트 PR#66@5d2d2de 결정표 R1~R5)의 5가지 측정-계약 결함을 반영했다:
- R1: 완료 보고 제출(state→review + completed_at 필드)과 최종 검증(state→done + completed_at
  필드)을 이벤트로 페어링 — completed_at은 verify에서 기록되고 verified_at은 보고 접수 시에도
  기록되므로 스냅샷 completed_at을 분모로 쓰면 대기 보고가 누락된다(server/routers/issues.py:309-329,
  server/service.py:197-199 실측 계약).
- R2: stall episode를 영속 상태 파일로 추적 — /lease·/ping은 SQL만 갱신하고 이벤트를 기록하지
  않으므로(라이브 실측 event delta=0) 이벤트만으로 복구를 못 본다. 관측 주기는 판정 목표(2시간)보다
  짧은 30분 크론을 전제로 하고, 미복구 episode는 우절단(censoring)으로 분리 표기한다.
- R3: review→todo 반납은 이전 상태가 review일 때만 — 이벤트 seq 순서로 카드별 이전 상태를
  추적하고, 로그 시작 이전 이력으로 이전 상태를 모르면 unknown으로 분리한다.
- R4: 착수시간은 todo 진입 → 첫 수령 — created_at→started_at이 아니라, todo 진입 이벤트
  (또는 생성 시 state=todo면 생성 시각)과 첫 started_at 필드 이벤트의 차. 실험 창 내 todo
  진입 코호트만 p50/p90 대상이며, 코호트 원시 행을 JSON에 보존한다.
- R5: blocked 진입만 집계 — payload의 state는 변경 후 전체 상태이므로 fields에 state가
  있고 이전 상태가 blocked가 아닐 때만 센다. 자동 blocked와 사람 개입은 v1 proxy로 구분 불가
  (출력에 명시).

계약(카드 본문): 착수시간·리뷰통과율·재작업률·stall복구율·개입횟수 5종. 카드 생성 수는 지표에서 제외.

사용: python3 metrics_collect.py [--base URL] [--json] [--out FILE] [--state-file FILE] [--window-start ISO]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta

STALL_HEARTBEAT_MIN = 60       # 스냅샷 stall 판정 기준(초안)
DEFAULT_RUN_INTERVAL_MIN = 30  # cron 권장 주기 — 복구 판정 목표(2시간)보다 짧아야 한다
HUMAN_ACTORS = ("skshim",)     # 사람 식별 접두 — 실험 기간에 확정


def get(base: str, path: str, timeout: int = 30):
    with urllib.request.urlopen(base.rstrip("/") + path, timeout=timeout) as r:
        return json.loads(r.read().decode())


def parse_ts(s):
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
            return sorted(events, key=lambda e: e["seq"])
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


def load_state(path):
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {"open": {}, "resolved": []}


def save_state(path, st):
    if path:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=2)


def payload_of(e):
    try:
        return json.loads(e.get("payload") or "{}")
    except json.JSONDecodeError:
        return {}


def collect(base: str, state_file: str, window_start: datetime):
    now = datetime.now().astimezone()
    issues = get(base, "/issues?limit=1000")
    if isinstance(issues, dict):
        issues = issues.get("issues", [])
    snap = {it["id"]: it for it in issues}
    events = fetch_events(base)

    last_state = {}   # 이슈별 이벤트에서 관측한 마지막 상태
    todo_entry = {}   # 이슈별 마지막 todo 진입 시각(첫 수령 전 기준)
    first_claim = {}  # 이슈별 첫 started_at 필드 이벤트 시각
    born_todo = {}    # 생성 시 state=todo였던 이슈 → 생성 이벤트 ts
    submissions = {}  # 이슈별 마지막 보고 제출 시각(state→review + completed_at 필드)
    verify_dones = [] # (이슈, ts) state→done + completed_at 필드
    todo_into = {"review": 0, "unknown_prev": 0}  # review=재작업 반납, 그 외 출발 상태는 개별 카운트
    blocked_entries = 0

    for e in events:  # seq 오름차순 보장(fetch_events)
        eid, ts, kind = e.get("entity_id"), parse_ts(e.get("ts")), e.get("kind")
        if kind == "issue.created":
            pl = payload_of(e)
            if pl.get("state") == "todo":
                born_todo[eid] = ts
                todo_entry.setdefault(eid, ts)
            last_state.setdefault(eid, pl.get("state"))
            continue
        if kind != "issue.updated":
            continue
        pl = payload_of(e)
        fields = pl.get("fields", [])
        st = pl.get("state")
        if "started_at" in fields and eid not in first_claim:
            first_claim[eid] = ts
        if st and "state" in fields:
            prev = last_state.get(eid)
            if st == "todo":
                if prev is None:
                    todo_into["unknown_prev"] += 1
                elif prev == "review":
                    todo_into["review"] += 1
                else:
                    todo_into[prev] = todo_into.get(prev, 0) + 1
                if eid not in first_claim:  # 첫 수령 전 최신 todo 진입만 착수시간 소스
                    todo_entry[eid] = ts
            elif st == "review" and "completed_at" in fields:
                submissions[eid] = ts  # 완료 보고 제출(서버 계약 실측)
            elif st == "done" and "completed_at" in fields:
                verify_dones.append((eid, ts))
            elif st == "blocked" and prev != "blocked":
                blocked_entries += 1  # R5: 실제 blocked 진입만
            last_state[eid] = st

    # ── R4 착수시간: todo 진입 → 첫 수령 (실험 창 내 todo 진입 코호트) ──
    cohort, excluded_unknown = [], 0
    for eid, fc in first_claim.items():
        te = todo_entry.get(eid)
        source = "event"
        if te is None and eid in born_todo:
            te, source = born_todo[eid], "born_todo"
        elif te is None and eid in snap:
            # 로그 시작 이전 출생(생성 이벤트 없음) — created_at 추정분은 별도 표기
            te, source = parse_ts(snap[eid].get("created_at")), "pre_log_estimated"
        if te is None:
            excluded_unknown += 1
            continue
        if te >= window_start:  # 실험 창 내 todo 진입 코호트만
            cohort.append({"id": eid, "todo_entry": te.isoformat(timespec="seconds"),
                           "first_claim": fc.isoformat(timespec="seconds"),
                           "minutes": round((fc - te).total_seconds() / 60.0, 1),
                           "source": source})
    t2s = [c["minutes"] for c in cohort]

    # ── R1 리뷰통과율: 제출/검증 이벤트 페어링 ──
    timeline = sorted([(ts, eid) for eid, ts in submissions.items()]
                      + [(ts, eid) for eid, ts in verify_dones], key=lambda x: x[0])
    marks = set((eid, ts) for eid, ts in submissions.items())
    sub_q, passed, direct = {}, [], 0
    for ts, eid in timeline:
        if (eid, ts) in marks:
            sub_q.setdefault(eid, []).append(ts)
        else:
            if sub_q.get(eid):
                passed.append((ts - sub_q[eid].pop(0)).total_seconds() / 60.0)
            else:
                direct += 1  # 제출 없는 done — close 라벨/force_done 승인 경로
    pending, reworked_submit = 0, 0
    for eid, tss in sub_q.items():
        cur = snap.get(eid, {}).get("state")
        for _ in tss:
            if cur == "review":
                pending += 1
            elif cur not in ("review", "done"):
                reworked_submit += 1  # 제출 후 반납(미검증 종료)
    lat = passed

    # ── R3 재작업률: attempt 스냅샷 + 이벤트 반납 병기 ──
    started = [it for it in issues if it.get("started_at")]
    reworked_n = [it for it in started if (it.get("execution_attempt") or 1) >= 2]

    # ── R2 stall episode (영속 상태 파일 + 스냅샷 판정) ──
    # in_progress: heartbeat 정지/lease 만료가 stall. review: 병합 대기는 정상 상태라
    # lease가 실제 만료된 경우만(heartbeat은 review에서 갱신되지 않는다).
    st = load_state(state_file)
    active = [it for it in issues if it.get("state") in ("in_progress", "review")]
    stalled_now = []
    for it in active:
        hb = parse_ts(it.get("heartbeat_at"))
        lease = parse_ts(it.get("lease_expires"))
        lease_expired = lease is not None and lease < now
        if it.get("state") == "in_progress":
            stale_hb = hb is None or (now - hb) > timedelta(minutes=STALL_HEARTBEAT_MIN)
            if stale_hb or lease_expired:
                stalled_now.append(it["id"])
        elif lease_expired:
            stalled_now.append(it["id"])
    resolved, still_open = [], {}
    for eid, ep in (st.get("open") or {}).items():
        if eid in stalled_now:
            ep["last_stalled"] = now.isoformat(timespec="seconds")
            still_open[eid] = ep
        else:
            cur = snap.get(eid, {}).get("state")
            ep["resolved_at"] = now.isoformat(timespec="seconds")
            ep["resolved_state"] = cur
            ep["recovered"] = cur is not None  # stall 집합 이탈 = 진행/종료 신호
            fs = parse_ts(ep.get("first_seen"))
            ep["minutes_open"] = round((now - fs).total_seconds() / 60.0, 1) if fs else None
            resolved.append(ep)
    for eid in stalled_now:
        if eid not in still_open and eid not in (st.get("open") or {}):
            still_open[eid] = {"first_seen": now.isoformat(timespec="seconds"),
                               "last_stalled": now.isoformat(timespec="seconds")}
    st = {"open": still_open, "resolved": (st.get("resolved") or []) + resolved}
    save_state(state_file, st)
    recovered = [r for r in st["resolved"] if r.get("recovered")]
    unrecovered = [r for r in st["resolved"] if not r.get("recovered")]

    # ── R5 개입 원시 카운트(사람 코멘트/메시지) ──
    human_comments, human_messages = 0, 0
    for e in events:
        if e.get("kind") in ("comment.added", "message.posted"):
            if any(payload_of(e).get("author", "").startswith(h) for h in HUMAN_ACTORS):
                if e["kind"] == "comment.added":
                    human_comments += 1
                else:
                    human_messages += 1

    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "window_start": window_start.isoformat(timespec="seconds"),
        "window_note": f"이벤트 로그 seq 1~{events[-1]['seq'] if events else 0} (로그 시작 이전 이력은 원시 근거 없음 — 집계 제외·별도 표기)",
        "issues_scanned": len(issues),
        "events_scanned": len(events),
        "time_to_start": {"p50_min": pctl(t2s, 0.5), "p90_min": pctl(t2s, 0.9),
                          "cohort_n": len(cohort), "excluded_unknown_todo_entry": excluded_unknown,
                          "definition": "todo 진입→첫 수령, 실험 창 내 todo 진입 코호트",
                          "cohort_rows": cohort},
        "review_pass": {"definition": "완료 보고 제출(state→review+completed_at 필드) 대비 검증 통과(state→done+completed_at 필드, 이벤트 페어링)",
                        "submitted": len(submissions), "passed": len(passed),
                        "pending": pending, "reworked_after_submit": reworked_submit,
                        "direct_done_no_submission": direct,
                        "pass_rate_pct": round(len(passed) / len(submissions) * 100.0, 1) if submissions else None,
                        "latency_p50_min": pctl(lat, 0.5)},
        "rework": {"attempt_ge2": len(reworked_n), "started": len(started),
                   "attempt_rate_pct": round(len(reworked_n) / len(started) * 100.0, 1) if started else None,
                   "review_to_todo_returns": todo_into.get("review", 0),
                   "todo_entries_by_prev_state": {k: v for k, v in todo_into.items() if k != "review"},
                   "note": "unknown_prev = 로그 시작 이전 이력으로 출발 상태 미상"},
        "stall": {"threshold_min": STALL_HEARTBEAT_MIN, "run_interval_assumed_min": DEFAULT_RUN_INTERVAL_MIN,
                  "active": len(active), "stalled_now": stalled_now,
                  "episodes_open": len(still_open),
                  "episodes_recovered": len(recovered),
                  "episodes_closed_unrecovered": len(unrecovered),
                  "recovery_rate_pct": round(len(recovered) / (len(recovered) + len(unrecovered)) * 100.0, 1) if (recovered or unrecovered) else None,
                  "resolved_rows": st["resolved"][-20:],
                  "note": "복구 시각은 관측 주기 상한(±run interval) — /lease·/ping은 이벤트 미기록(server 실측)이라 스냅샷 주기 기반"},
        "intervention": {"human_comments": human_comments, "human_messages": human_messages,
                         "blocked_entries": blocked_entries,
                         "note": "자동 blocked vs 사람 개입 구분 불가 — v1 proxy"},
    }


def dashboard(m: dict) -> str:
    t2s, rp, rw, stl, iv = m["time_to_start"], m["review_pass"], m["rework"], m["stall"], m["intervention"]
    lines = [
        "## TT 지표 대시보드 (v2)",
        f"- 생성: {m['generated_at']} | 창 시작: {m['window_start']} | 스캔: 카드 {m['issues_scanned']} / 이벤트 {m['events_scanned']}",
        "",
        "| 지표 | 값 | 근거 |",
        "|---|---|---|",
        f"| 착수시간 p50 (todo진입→첫수령, 창 내 코호트) | {fmt_min(t2s['p50_min'])} | n={t2s['cohort_n']} (제외 {t2s['excluded_unknown_todo_entry']}) |",
        f"| 착수시간 p90 | {fmt_min(t2s['p90_min'])} | n={t2s['cohort_n']} |",
        f"| 리뷰통과율 (제출→검증 페어링) | {str(rp['pass_rate_pct']) + '%' if rp['pass_rate_pct'] is not None else '-'} | {rp['passed']}/{rp['submitted']} (대기 {rp['pending']}, 반납 {rp['reworked_after_submit']}, 직행 {rp['direct_done_no_submission']}) |",
        f"| 검증 지연 p50 | {fmt_min(rp['latency_p50_min'])} | n={rp['passed']} |",
        f"| 재작업률 (attempt≥2) | {str(rw['attempt_rate_pct']) + '%' if rw['attempt_rate_pct'] is not None else '-'} | {rw['attempt_ge2']}/{rw['started']} |",
        f"| review→todo 반납 | {rw['review_to_todo_returns']}회 | events(이전 상태 추적) |",
        f"| todo 진입 출발 상태 | {json.dumps(rw['todo_entries_by_prev_state'], ensure_ascii=False)} | events |",
        f"| stall 현재/episode | {len(stl['stalled_now'])}/{stl['active']} active | 열림 {stl['episodes_open']} · 복구 {stl['episodes_recovered']} · 미복구 {stl['episodes_closed_unrecovered']} |",
        f"| stall복구율 (해소분 기준) | {str(stl['recovery_rate_pct']) + '%' if stl['recovery_rate_pct'] is not None else '-'} | 상태 파일 누적 |",
        f"| 개입: 사람 코멘트/메시지/blocked 진입 | {iv['human_comments']}/{iv['human_messages']}/{iv['blocked_entries']} | events (v1 proxy) |",
        "",
        f"stall 상세: {', '.join(stl['stalled_now'][:5]) or '없음'}",
    ]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.environ.get("TT_URL", "http://localhost:7800"), help="TT 서버 URL — 운영 주소는 환경변수 TT_URL 또는 이 플래그로 전달(레포에 기록하지 않는다)")
    ap.add_argument("--json", action="store_true", help="JSON만 출력(코호트·episode 원시 행 포함)")
    ap.add_argument("--out", help="결과 파일 저장 경로")
    ap.add_argument("--state-file", default="tt_metrics_state.json", help="stall episode 영속 상태 파일(실행 호스트, Git 밖)")
    ap.add_argument("--window-start", default=None, help="실험 창 시작 ISO(기본: 이벤트 로그 시작)")
    args = ap.parse_args()

    first = get(args.base, "/events?limit=1").get("events", [])
    ws = parse_ts(args.window_start) if args.window_start else (
        parse_ts(first[0]["ts"]) if first else None) or datetime.now().astimezone()
    m = collect(args.base, args.state_file, ws)
    text = json.dumps(m, ensure_ascii=False, indent=2) if args.json else dashboard(m)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    print(text)


if __name__ == "__main__":
    sys.exit(main())
