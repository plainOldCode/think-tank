#!/usr/bin/env python3
"""VMS0(M53PBEWX-VMS0) — 카드당 기계 댓글 수·비율 측정.

WY6G는 CLI 표시만 바꿨고 감소 측정은 후속이었다. 이 스크립트는 TT API에서 카드별
기계/전체 댓글을 집계해 전후 비교 창구로 쓴다. 기계 판정 규칙은 cli/tt의 jq
`def machine`과 동일: ^(probe|tt-server|tt-embedding-daemon|board)$|^runner@.

사용: python3 machine_comment_ratio.py --base http://127.0.0.1:7800 [--days 30] [--top 10] [--json]
"""
from __future__ import annotations

import argparse
import json
import re
import time
import urllib.parse
import urllib.request

MACHINE_RE = re.compile(r"^(probe|tt-server|tt-embedding-daemon|board)$|^runner@")


def is_machine(author: str) -> bool:
    return bool(MACHINE_RE.match(author or ""))


def summarize(issues: list) -> dict:
    """카드 목록 → 기계 댓글 집계. 순수 함수 — 테스트 대상."""
    total = mc = 0
    per_card = []
    for i in issues:
        comments = i.get("comments") or []
        n = len(comments)
        m = sum(1 for c in comments if is_machine(c.get("author")))
        total += n
        mc += m
        if n:
            per_card.append({"id": i.get("id"), "machine": m, "total": n,
                             "ratio": round(m / n, 3) if n else 0.0})
    per_card.sort(key=lambda x: (-x["machine"], -x["total"], x["id"]))
    return {"cards": len(issues), "comments": total, "machine": mc,
            "machine_ratio": round(mc / total, 3) if total else 0.0,
            "worst": per_card[:10]}


def _get(base: str, path: str, timeout: int = 30):
    with urllib.request.urlopen(base.rstrip("/") + path, timeout=timeout) as r:
        return json.load(r)


def fetch_issues(base: str, days: int, limit: int = 500) -> list:
    cutoff = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(time.time() - days * 86400))
    q = urllib.parse.urlencode({"archived": "active", "limit": limit})
    issues = _get(base, f"/issues?{q}")
    out = []
    for i in issues:
        full = _get(base, f"/issues/{i['id']}")
        comments = [c for c in (full.get("comments") or []) if (c.get("ts") or "") >= cutoff]
        out.append({"id": i["id"], "comments": comments})
    return out


def main() -> int:
    p = argparse.ArgumentParser(description="카드당 기계 댓글 수·비율 (VMS0)")
    p.add_argument("--base", default="http://127.0.0.1:7800")
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--top", type=int, default=10)
    p.add_argument("--json", action="store_true")
    a = p.parse_args()
    issues = fetch_issues(a.base, a.days)
    s = summarize(issues)
    s["worst"] = s["worst"][:a.top]
    s["window_days"] = a.days
    if a.json:
        print(json.dumps(s, ensure_ascii=False, indent=1))
        return 0
    print("기계 댓글 비율: %d/%d = %.1f%% (카드 %d장, 최근 %d일)"
          % (s["machine"], s["comments"], s["machine_ratio"] * 100, s["cards"], a.days))
    for w in s["worst"]:
        print("  %s: 기계 %d/전체 %d (%.0f%%)" % (w["id"], w["machine"], w["total"], w["ratio"] * 100))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
