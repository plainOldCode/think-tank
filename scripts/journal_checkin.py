#!/usr/bin/env python3
"""에이전트 일지 체크인 — 유휴 시 일지 카드에 상태 코멘트 (M4580A48-573W/C9B0).

사용: TT_URL=http://<tt>:7800 python3 journal_checkin.py <agent>
- 본인 명의 활성 lease(lease_by=agent, lease_expires>now)가 있으면 스킵 — 바쁜 에이전트는 기록하지 않는다.
- 일지 카드(라벨 일지, 제목 '일지: <agent>')가 없으면 claim 없이 생성: new → PATCH(state, assignee).
  claim을 쓰면 본인 lease를 점유해 유휴 정의·lease 2 한도를 깨므로 절대 claim하지 않는다.
- 체크인 코멘트 3블록: 한 일(마지막 체크인 이후 본인 author 코멘트가 있는 카드), 할 일(활성 lease 카드),
  지시사항(일지 카드에 책임자가 마지막 체크인 이후 남긴 코멘트).
- 크론에서 pull을 절대 쓰지 않는다(--label auto 게이트와 무관하게 note만).
"""
import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

URL = os.environ.get("TT_URL", "http://100.122.96.16:7800").rstrip("/")
AG = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("TT_AGENT", "")
HEADERS = {"content-type": "application/json", "x-agent": AG or "journal-cron"}
CHIEFS = ("tp-13", "hermes", "scott")   # 지시사항으로 구속되는 author 접두


def api(path, payload=None, method=None, timeout=30):
    data = json.dumps(payload).encode() if payload is not None else None
    r = urllib.request.Request(URL + path, data=data, method=method or ("POST" if data else "GET"),
                               headers=HEADERS)
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def now():
    return datetime.now(timezone.utc).astimezone()


def ts(s):
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def main():
    if not AG:
        sys.exit("사용법: journal_checkin.py <agent> (또는 TT_AGENT)")
    issues = api("/issues?limit=500")
    nowdt = now()
    leases = [i for i in issues if i.get("lease_by") == AG and ts(i.get("lease_expires") or "") and
              ts(i["lease_expires"]) > nowdt]
    journal = next((i for i in issues if "일지" in (i.get("labels") or [])
                    and i["title"].strip() == f"일지: {AG}"), None)

    if journal is None:
        j = api("/issues", {"title": f"일지: {AG}", "labels": ["일지"],
                            "body": f"{AG}의 유휴 체크인 스레드 — 에이전트 일지 규약 v1. 이 카드는 claim/done 대상이 아니다."})
        j = api(f"/issues/{j['id']}", {"state": "in_progress", "assignee": AG, "version": j["version"]}, "PATCH")
        journal = j
        print(f"일지 카드 신설: {j['id']}")

    jid = journal["id"]
    jcomments = journal.get("comments") or []
    mine = [ts(c["ts"]) for c in jcomments if c["author"] == AG]
    last = max(mine) if mine else None

    if leases:
        print(f"{AG}: 활성 lease {len(leases)}건 — 바쁨, 체크인 스킵")
        return

    # 한 일: 마지막 체크인 이후 내 코멘트가 있는 카드 (최근 3건)
    did = []
    if last:
        for i in issues:
            cs = [c for c in (i.get("comments") or []) if c["author"] == AG and ts(c["ts"]) and ts(c["ts"]) > last]
            if cs:
                did.append(f"{i['id']} {i['title'][:30]}")
    did = did[-3:] if did else ["(기록 없음)"]

    # 할 일: 활성 lease 카드 — 유휴므로 사실상 없음, 명시가 목적
    todo = [f"{i['id']} {i['title'][:30]}" for i in leases] or ["없음 — 유휴"]

    # 지시사항: 마지막 체크인 이후 책임자가 일지 카드에 남긴 것
    orders = [c["body"][:200] for c in jcomments
              if any(c["author"].startswith(p) for p in CHIEFS) and c["author"] != AG
              and ts(c["ts"]) and (not last or ts(c["ts"]) > last)]
    orders = orders[-3:] or ["없음"]

    body = (f"체크인 {nowdt.isoformat(timespec='seconds')}\n\n"
            f"**한 일**: " + "; ".join(did) + "\n"
            f"**할 일**: " + "; ".join(todo) + "\n"
            f"**지시사항**: " + "; ".join(orders))
    api(f"/issues/{jid}/comments", {"author": AG, "body": body})
    print(f"{AG}: 체크인 기록 → {jid}")


if __name__ == "__main__":
    main()
