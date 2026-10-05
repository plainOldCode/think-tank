"""일지 체크인 서버 내장화 (M4580RJK-C9B0) — 외부 크론/스크립트 없이 probe 사이클이 수행.

규약 v1 (M4580RF9-JXR4): 에이전트별 스레드 카드 '일지: <agent>', 라벨 '일지',
state in_progress(영구 스레드), claim/done 대상 아님. 체크인은 pull/claim 없이 코멘트만.
- 유휴 정의: 본인 명의 활성 lease 0개 — 보유 중이면 기록하지 않는다(lease 소모·점유 금지).
- 주기 게이트: 일지 카드의 본인 마지막 체크인이 TT_JOURNAL_HOURS(기본 6h) 이내면 스킵.
  별도 상태 저장 없이 카드 코멘트에서 유도 — 서버 재시작에도 유지.
- 체크인 본문 3블록: 한 일(마지막 체크인 이후 본인 author 코멘트가 있는 카드),
  할 일(유휴 체크인이므로 상시 '없음 — 유휴'), 지시사항(책임자가 일지 카드에 남긴 최근 코멘트).
"""
import os
import urllib.parse
from datetime import datetime, timedelta


def _ts(s):
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


CHIEFS = ("tp-13", "hermes", "scott")  # 지시사항으로 구속되는 author 접두


def _compose(agent, jid, issues, jcomments, now, last):
    did = []
    for i in issues:
        if i["id"] == jid:
            continue
        cs = [c for c in (i.get("comments") or [])
              if c["author"] == agent and _ts(c.get("ts")) and (not last or _ts(c["ts"]) >= last)]
        if cs:
            did.append(f"{i['id']} {i['title'][:30]}")
    did = did[-3:] if did else ["(기록 없음)"]

    orders = [c["body"][:200] for c in jcomments
              if any(c["author"].startswith(p) for p in CHIEFS) and c["author"] != agent
              and _ts(c.get("ts")) and (not last or _ts(c["ts"]) >= last)]
    orders = orders[-3:] or ["없음"]

    return (f"체크인 {now.strftime('%Y-%m-%dT%H:%M:%S%z')}\n\n"
            f"**한 일**: " + "; ".join(did) + "\n"
            f"**할 일**: 없음 — 유휴\n"
            f"**지시사항**: " + "; ".join(orders))


def journal_tick(url, hours=None, now=None):
    """등록 에이전트 순회 — 유휴 에이전트의 일지 카드를 확보하고 체크인 코멘트를 기록한다.

    반환: 실행된 액션 목록([{action: journal-new|journal-checkin, issue, agent}]).
    """
    from probe import core  # 순환 회피 — 런타임에만 (테스트가 core.api를 교체한다)

    hours = int(os.getenv("TT_JOURNAL_HOURS", "6")) if hours is None else hours
    if hours <= 0:
        return []
    now = now or datetime.now().astimezone()
    issues = core.api(url, "/issues?limit=500&with_comments=1")
    agents = core.api(url, "/agents")
    # codex F2: created_at DESC limit=500은 일지·lease를 누락한다 — 라벨/lease_by 필터로 정확히 조회.
    journals = core.api(url, "/issues?label=%EC%9D%BC%EC%A7%80&limit=1000&with_comments=1")
    out = []
    for ag in agents:
        if not ag.get("enabled", True):
            continue
        name = ag["name"]
        if any((_ts(i.get("lease_expires")) or now) > now
               for i in core.api(url, f"/issues?lease_by={urllib.parse.quote(name)}&limit=10")):
            continue  # 바쁨 — 유휴가 아니면 기록하지 않는다
        journal = next((i for i in journals
                        if i["title"].strip() == f"일지: {name}"), None)
        if journal is None:
            j = core.api(url, "/issues", "POST", {
                "title": f"일지: {name}", "labels": ["일지"],
                "body": f"{name}의 유휴 체크인 스레드 — 에이전트 일지 규약 v1. 이 카드는 claim/done 대상이 아니다."})
            j = core.api(url, f"/issues/{j['id']}", "PATCH",
                         {"state": "in_progress", "assignee": name, "version": j["version"]})
            journal = j
            out.append({"action": "journal-new", "issue": j["id"], "agent": name})
        jcomments = journal.get("comments") or []
        last = max((_ts(c["ts"]) for c in jcomments
                    if c["author"] == name and _ts(c.get("ts"))), default=None)
        if last and last > now - timedelta(hours=hours):
            continue  # 주기 게이트 — 최근 체크인 있음
        core.api(url, f"/issues/{journal['id']}/comments", "POST",
                 {"author": name, "body": _compose(name, journal["id"], issues, jcomments, now, last)})
        out.append({"action": "journal-checkin", "issue": journal["id"], "agent": name})
    return out
