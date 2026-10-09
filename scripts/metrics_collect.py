#!/usr/bin/env python3
"""TT 지표 수집기 v4 — TT 개선#3e (M4FFCB9J-2AAB) 2주 실험 측정 창구.

v4는 3차 독립 리뷰(PR#66@45eb208, R1~R4)의 경계 결함을 반영했다:
- R1: 제출 회차 종료는 창 무관 — 창 이전 반납도 미검증 제출을 종료한다. v3는 종료
  자체를 창 조건 안에 넣어 "창 이전 제출→반납→재수령→force_done"이 이전 제출과
  페어링되는 오분류를 만들었다. 집계(반납·직행 카운트)만 창으로 제한한다.
- R2: 120분 복구율 분모는 시간값이 유효한 전체 해소분 — v3는 <=120 필터로 분모를
  좁혀 느린 복구(180분)가 판정에서 사라져 100%로 과대평가됐다.
- R3: 반납·출발 상태 카운트도 창 안에서만 누적 — v3는 todo_into 누적이 창 검사
  밖에 있어 이전 주 반납이 섞였다. 상태 복원은 로그 전체, 카운트만 창 내.
- R4: todo_entry는 실제 todo 진입(fields에 state)에서만 갱신 — v3는 갱신을 fields
  조건 밖으로 빼서 todo 카드의 priority/title 수정이 진입 시각을 덮어썼다.

v2~v3에서 유지: 제출 인스턴스 FIFO+대체 종료+동초 상태값 분류(2차 R1), 해소 결과
분류+식별자 보존(2차 R2), 전 state-bearing 이벤트 last_state 갱신(2차 R3), claim·pull
공통 첫 수령(2차 R4), blocked 실제 진입만(1차 R5). parse_ts는 Python 3.9 호환을 위해
'Z' 접미를 정규화한다(이전 통합 리뷰의 3.9 블로커).

계약(카드 본문): 착수시간·리뷰통과율·재작업률·stall복구율·개입횟수 5종. 카드 생성 수는
지표에서 제외.

사용: python3 metrics_collect.py [--base URL] [--json] [--out FILE] [--state-file FILE] [--window-start ISO]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta

STALL_HEARTBEAT_MIN = 60       # 스냅샷 stall 판정 기준(초안)
DEFAULT_RUN_INTERVAL_MIN = 30  # cron 권장 주기 — 복구 판정 목표(2시간)보다 짧아야 한다
HUMAN_ACTORS = ("skshim",)     # 사람 식별 접두 — 실험 기간에 확정
RECOVERY_JUDGE_MIN = 120       # §5-4 판정용 복구 소요 상한(분)
RECOVERED_STATES = ("in_progress", "review", "done")  # 복구로 인정하는 해소 상태


def get(base: str, path: str, timeout: int = 30):
    with urllib.request.urlopen(base.rstrip("/") + path, timeout=timeout) as r:
        return json.loads(r.read().decode())


def parse_ts(s):
    """ISO 타임스탬프 파싱 — Python 3.9 fromisoformat 호환 정규화.

    서버 db.now()/future()는 %z 형식이라 'Z'와 콜론 없는 '+0900' 오프셋을 모두
    보낸다(server/db.py:139-144 실측). 3.9의 fromisoformat은 둘 다 ValueError이므로
    'Z'→'+00:00', '+0900'→'+09:00'로 정규화한다.
    """
    if not s:
        return None
    if isinstance(s, str):
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        m = re.search(r"([+-]\d{2})(\d{2})$", s)
        if m:
            s = s[:m.start()] + m.group(1) + ":" + m.group(2)
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

    last_state = {}    # 이슈별 마지막 관측 상태 — 모든 state-bearing 이벤트에서 갱신(R3)
    todo_entry = {}    # 이슈별 todo 진입 시각(첫 수령 전 최신값, 첫 수령 후 동결 — R4)
    first_receipt = {} # 이슈별 첫 수령 시각 — state→in_progress 전이 또는 started_at 필드(R4)
    born_todo = {}     # 생성 시 state=todo였던 이슈 → 생성 이벤트 ts
    pending_subs = {}  # 이슈별 미검증 제출 인스턴스 FIFO [(seq, ts)] (R1)
    sub_events = []    # (seq, eid, ts) 제출 이벤트 전체
    pass_pairs = []    # (eid, submit_ts, verify_ts) 페어링 결과
    direct_done = 0    # 제출 없는 done — close 라벨/force_done 승인 경로(창 내만)
    reworked_after_submit = 0  # 반납으로 종료된 제출 인스턴스(창 내 이벤트만)
    superseded = 0     # 대체 제출로 종료된 이전 회차(R1)
    todo_into = {"review": 0, "unknown_prev": 0}
    blocked_entries = 0

    for e in events:  # seq 오름차순 보장(fetch_events)
        eid, ts, kind = e.get("entity_id"), parse_ts(e.get("ts")), e.get("kind")
        if kind == "issue.created":
            pl = payload_of(e)
            if pl.get("state") == "todo" and ts:
                born_todo[eid] = ts
                todo_entry.setdefault(eid, ts)
            last_state.setdefault(eid, pl.get("state"))
            continue
        if kind != "issue.updated":
            continue
        pl = payload_of(e)
        fields = pl.get("fields", [])
        st = pl.get("state")
        if not st:
            continue
        prev = last_state.get(eid)
        is_field_change = "state" in fields  # 실제 전이(PATCH). /pull은 fields 없음(서버 실측)
        if eid not in first_receipt and ts and (st == "in_progress" or "started_at" in fields):
            first_receipt[eid] = ts  # R4: claim·pull 공통 첫 수령
        if st == "todo":
            if is_field_change:
                closed = len(pending_subs.get(eid, []))
                if ts and ts >= window_start:
                    if prev is None:
                        todo_into["unknown_prev"] += 1
                    elif prev == "review":
                        todo_into["review"] += 1
                    else:
                        todo_into[prev] = todo_into.get(prev, 0) + 1
                    reworked_after_submit += closed  # R1/R3: 반납 집계에 실험 창 필터
                pending_subs[eid] = []  # R1: 회차 종료는 창 무관 — 창 이전 반납도 종료해야 함
                if eid not in first_receipt:  # 실제 todo 진입만 착수 소스(메타데이터 수정 무시 — R4), 이후 동결
                    todo_entry[eid] = ts
        elif st == "review" and "completed_at" in fields:
            # R1: 대체 제출 — 같은 카드에 미검증 제출이 남아 있으면 회차 종료(superseded)
            if ts and ts >= window_start:
                superseded += len(pending_subs.get(eid, []))
            pending_subs[eid] = []
            sub_events.append((e["seq"], eid, ts))  # 완료 보고 제출(서버 계약 실측)
            pending_subs.setdefault(eid, []).append((e["seq"], ts))
        elif st == "done" and "completed_at" in fields:
            if pending_subs.get(eid):
                _, s_ts = pending_subs[eid].pop(0)  # R1: 가장 오래된 미검증 제출과 페어링
                pass_pairs.append((eid, s_ts, ts))
            elif ts and ts >= window_start:
                direct_done += 1  # 창 내 제출 없는 done만 직행 카운트(R1)
        elif st == "blocked" and is_field_change and prev != "blocked":
            blocked_entries += 1  # R5: 실제 blocked 진입만
        last_state[eid] = st  # R3: fields 유무와 무관하게 관측 상태 갱신

    # ── R4 착수시간: todo 진입 → 첫 수령 (실험 창 내 todo 진입 코호트) ──
    cohort, excluded_unknown = [], 0
    for eid, fr in first_receipt.items():
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
                           "first_receipt": fr.isoformat(timespec="seconds"),
                           "minutes": round((fr - te).total_seconds() / 60.0, 1),
                           "source": source})
    t2s = [c["minutes"] for c in cohort]

    # ── R1 리뷰통과율: 제출 인스턴스 페어링 + 창 필터 ──
    submitted_w = [s for s in sub_events if s[2] >= window_start]
    passed_w = [(eid, s_ts, v_ts) for (eid, s_ts, v_ts) in pass_pairs if v_ts >= window_start]
    passed_pre_submit = [p for p in pass_pairs if p[1] < window_start <= p[2]]
    lat = [(v_ts - s_ts).total_seconds() / 60.0 for (_, s_ts, v_ts) in passed_w]
    leftover_open, leftover_other = 0, 0
    for eid, q in pending_subs.items():
        cur = snap.get(eid, {}).get("state")
        if cur == "review":
            leftover_open += len(q)  # 아직 검증 대기
        else:
            leftover_other += len(q)  # 로그 밖 경로로 종료(cancelled 등) — 분류 별도

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
            ep["id"] = eid
            ep["last_stalled"] = now.isoformat(timespec="seconds")
            still_open[eid] = ep
        else:
            cur = snap.get(eid, {}).get("state")
            ep["id"] = eid  # R2: 해소 행에도 식별자 보존
            ep["resolved_at"] = now.isoformat(timespec="seconds")
            ep["resolved_state"] = cur
            if cur is None:
                ep["outcome"] = "missing"        # 관측 소실
            elif cur in RECOVERED_STATES:
                ep["outcome"] = "recovered"      # 진행 재개 또는 정상 종료
            elif cur == "todo":
                ep["outcome"] = "returned"       # 반납 — 복구 아님(R2)
            elif cur == "blocked":
                ep["outcome"] = "blocked"
            else:
                ep["outcome"] = f"other:{cur}"   # backlog/cancelled 등
            fs = parse_ts(ep.get("first_seen"))
            ep["minutes_open"] = round((now - fs).total_seconds() / 60.0, 1) if fs else None
            resolved.append(ep)
    for eid in stalled_now:
        if eid not in still_open and eid not in (st.get("open") or {}):
            still_open[eid] = {"id": eid, "first_seen": now.isoformat(timespec="seconds"),
                               "last_stalled": now.isoformat(timespec="seconds")}
    st = {"open": still_open, "resolved": (st.get("resolved") or []) + resolved}
    save_state(state_file, st)
    all_resolved = st["resolved"]
    recovered = [r for r in all_resolved if r.get("outcome") == "recovered"]
    unrecovered = [r for r in all_resolved if r.get("outcome") != "recovered"]
    # R2: 120분 복구율 — 분모는 시간값이 유효한 전체 해소분, 분자는 recovered && <=120분.
    # <=120로 분모를 좁히면 느린 복구가 판정에서 사라져 과대평가된다(3차 리뷰).
    timed = [r for r in all_resolved if r.get("minutes_open") is not None]
    recovered_120 = [r for r in timed if r.get("outcome") == "recovered"
                     and r["minutes_open"] <= RECOVERY_JUDGE_MIN]

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
        "collector_version": "v4",
        "generated_at": now.isoformat(timespec="seconds"),
        "window_start": window_start.isoformat(timespec="seconds"),
        "window_note": f"이벤트 로그 seq 1~{events[-1]['seq'] if events else 0} (로그 시작 이전 이력은 원시 근거 없음 — 집계 제외·별도 표기)",
        "issues_scanned": len(issues),
        "events_scanned": len(events),
        "time_to_start": {"p50_min": pctl(t2s, 0.5), "p90_min": pctl(t2s, 0.9),
                          "cohort_n": len(cohort), "excluded_unknown_todo_entry": excluded_unknown,
                          "definition": "todo 진입→첫 수령(claim·pull 공통), 실험 창 내 todo 진입 코호트",
                          "cohort_rows": cohort},
        "review_pass": {"definition": "완료 보고 제출(state→review+completed_at 필드) 대비 검증 통과(state→done+completed_at 필드, 인스턴스 FIFO 페어링)",
                        "submitted": len(submitted_w), "passed": len(passed_w),
                        "passed_with_pre_window_submit": len(passed_pre_submit),
                        "pending": leftover_open, "reworked_after_submit": reworked_after_submit,
                        "superseded_submissions": superseded,
                        "direct_done_no_submission": direct_done,
                        "leftover_closed_outside_log": leftover_other,
                        "pass_rate_pct": round(len(passed_w) / len(submitted_w) * 100.0, 1) if submitted_w else None,
                        "latency_p50_min": pctl(lat, 0.5),
                        "note": "rate는 창 내 제출 분모 기준 — 창 이전 제출이 창 내 검증되면 100% 초과 가능(passed_with_pre_window_submit로 분리)"},
        "rework": {"attempt_ge2": len(reworked_n), "started": len(started),
                   "attempt_rate_pct": round(len(reworked_n) / len(started) * 100.0, 1) if started else None,
                   "review_to_todo_returns": todo_into.get("review", 0),
                   "todo_entries_by_prev_state": {k: v for k, v in todo_into.items() if k != "review" and v > 0},
                   "note": "unknown_prev = 로그 시작 이전 이력으로 출발 상태 미상. 반납 카운트에 창 필터 적용"},
        "stall": {"threshold_min": STALL_HEARTBEAT_MIN, "run_interval_assumed_min": DEFAULT_RUN_INTERVAL_MIN,
                  "active": len(active), "stalled_now": stalled_now,
                  "episodes_open": len(still_open),
                  "episodes_recovered": len(recovered),
                  "episodes_closed_unrecovered": len(unrecovered),
                  "recovery_rate_pct": round(len(recovered) / len(all_resolved) * 100.0, 1) if all_resolved else None,
                  "recovery_rate_120m_pct": round(len(recovered_120) / len(timed) * 100.0, 1) if timed else None,
                  "timed_resolved_n": len(timed),
                  "resolved_rows": all_resolved[-20:],
                  "note": "복구=해소 시 in_progress/review/done. returned/blocked/missing/other는 미복구 분류. 120분 집계가 §5-4 판정 기준"},
        "intervention": {"human_comments": human_comments, "human_messages": human_messages,
                         "blocked_entries": blocked_entries,
                         "note": "자동 blocked vs 사람 개입 구분 불가 — v1 proxy"},
    }


def dashboard(m: dict) -> str:
    t2s, rp, rw, stl, iv = m["time_to_start"], m["review_pass"], m["rework"], m["stall"], m["intervention"]
    lines = [
        f"## TT 지표 대시보드 ({m['collector_version']})",
        f"- 생성: {m['generated_at']} | 창 시작: {m['window_start']} | 스캔: 카드 {m['issues_scanned']} / 이벤트 {m['events_scanned']}",
        "",
        "| 지표 | 값 | 근거 |",
        "|---|---|---|",
        f"| 착수시간 p50 (todo진입→첫수령, 창 내 코호트) | {fmt_min(t2s['p50_min'])} | n={t2s['cohort_n']} (제외 {t2s['excluded_unknown_todo_entry']}) |",
        f"| 착수시간 p90 | {fmt_min(t2s['p90_min'])} | n={t2s['cohort_n']} |",
        f"| 리뷰통과율 (제출→검증 페어링) | {str(rp['pass_rate_pct']) + '%' if rp['pass_rate_pct'] is not None else '-'} | {rp['passed']}/{rp['submitted']} (대기 {rp['pending']}, 반납 {rp['reworked_after_submit']}, 직행 {rp['direct_done_no_submission']}) |",
        f"| 검증 지연 p50 | {fmt_min(rp['latency_p50_min'])} | n={rp['passed']} |",
        f"| 재작업률 (attempt≥2) | {str(rw['attempt_rate_pct']) + '%' if rw['attempt_rate_pct'] is not None else '-'} | {rw['attempt_ge2']}/{rw['started']} |",
        f"| review→todo 반납 (창 내) | {rw['review_to_todo_returns']}회 | events(이전 상태 추적) |",
        f"| todo 진입 출발 상태 | {json.dumps(rw['todo_entries_by_prev_state'], ensure_ascii=False)} | events |",
        f"| stall 현재/episode | {len(stl['stalled_now'])}/{stl['active']} active | 열림 {stl['episodes_open']} · 복구 {stl['episodes_recovered']} · 미복구 {stl['episodes_closed_unrecovered']} |",
        f"| stall복구율 전체 (해소분 기준) | {str(stl['recovery_rate_pct']) + '%' if stl['recovery_rate_pct'] is not None else '-'} | 상태 파일 누적 |",
        f"| stall복구율 120분 이내 (판정 기준) | {str(stl['recovery_rate_120m_pct']) + '%' if stl['recovery_rate_120m_pct'] is not None else '-'} | 시간 유효 해소분 n={stl['timed_resolved_n']} |",
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
