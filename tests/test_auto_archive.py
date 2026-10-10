"""자동 아카이브 스윕(M3ZRGTSF-N95X) — decide 스케줄 + execute 조상 규칙."""
import pytest

import dispatchd
import probe.core


def _snap(issues=(), prs=(), agents=()):
    return {"auto": True, "now": "2026-10-10T10:00:00+0900",
            "agents": list(agents), "issues": list(issues),
            "prs": list(prs)}


def test_decide는_환경변수_설정시_스윕을_건다(monkeypatch):
    monkeypatch.setenv("TT_ARCHIVE_AFTER_DAYS", "14")
    acts = dispatchd.decide(_snap())
    sweeps = [a for a in acts if a["action"] == "archive-sweep"]
    assert len(sweeps) == 1 and sweeps[0]["days"] == 14


def test_decide는_환경변수_없으면_스윕을_안_건다(monkeypatch):
    monkeypatch.delenv("TT_ARCHIVE_AFTER_DAYS", raising=False)
    assert not [a for a in dispatchd.decide(_snap()) if a["action"] == "archive-sweep"]


class FakeApi:
    """카드 저장소 흉내 — GET /issues 목록+상세, PATCH archived 기록."""

    def __init__(self, cards):
        self.cards = {c["id"]: c for c in cards}
        self.patches = []

    def __call__(self, url, path, method="GET", body=None):
        if path.startswith("/issues?"):
            return [dict(c) for c in self.cards.values()
                    if c["state"] == "done" and not c.get("archived")]
        iid = path.split("/")[-1] if method == "PATCH" else path.split("/")[-1]
        if method == "PATCH":
            card = self.cards[iid]
            card.update(body or {})
            self.patches.append((iid, dict(body or {})))
            return dict(card)
        card = self.cards.get(iid)
        if card is None:
            raise RuntimeError("404")
        return dict(card)


OLD = "2026-09-20T09:00:00+0900"  # now(10-10)보다 14일 이상 전
RECENT = "2026-10-08T09:00:00+0900"


def test_스윕은_오래된_done만_아카이브한다(monkeypatch):
    fake = FakeApi([
        {"id": "OLD-1", "state": "done", "archived": 0, "version": 3,
         "completed_at": OLD, "parent_id": None},
        {"id": "RECENT-1", "state": "done", "archived": 0, "version": 2,
         "completed_at": RECENT, "parent_id": None},
    ])
    monkeypatch.setattr(probe.core, "api", fake)
    probe.core.execute("http://x", {"agent": "probe", "action": "archive-sweep", "days": 14})
    assert [p[0] for p in fake.patches] == ["OLD-1"]
    assert fake.patches[0][1] == {"archived": True, "version": 3}
    assert fake.cards["OLD-1"]["archived"] == 1


def test_미완_조상이_있으면_보존하고_조상_done이면_아카이브한다(monkeypatch):
    fake = FakeApi([
        {"id": "CHILD-1", "state": "done", "archived": 0, "version": 5,
         "completed_at": OLD, "parent_id": "EPIC-1"},
        {"id": "CHILD-2", "state": "done", "archived": 0, "version": 5,
         "completed_at": OLD, "parent_id": "EPIC-2"},
        {"id": "EPIC-1", "state": "backlog", "archived": 0, "version": 1,
         "completed_at": None, "parent_id": None},
        {"id": "EPIC-2", "state": "done", "archived": 0, "version": 9,
         "completed_at": OLD, "parent_id": "GRAND-1"},
        {"id": "GRAND-1", "state": "done", "archived": 0, "version": 2,
         "completed_at": OLD, "parent_id": None},
    ])
    monkeypatch.setattr(probe.core, "api", fake)
    probe.core.execute("http://x", {"agent": "probe", "action": "archive-sweep", "days": 14})
    # CHILD-1: 부모 backlog → 보존 / CHILD-2: 부모+조부 done → 아카이브.
    # EPIC-2·GRAND-1도 자체적으로 done+노후라 아카이브 대상.
    assert sorted(p[0] for p in fake.patches) == ["CHILD-2", "EPIC-2", "GRAND-1"]
    assert fake.cards["CHILD-1"]["archived"] == 0


def test_조상_유실은_보수적으로_보존한다(monkeypatch):
    fake = FakeApi([
        {"id": "ORPHAN-1", "state": "done", "archived": 0, "version": 1,
         "completed_at": OLD, "parent_id": "GONE-1"},
    ])
    monkeypatch.setattr(probe.core, "api", fake)
    probe.core.execute("http://x", {"agent": "probe", "action": "archive-sweep", "days": 14})
    assert fake.patches == []


def test_days_0이면_아무것도_안_한다(monkeypatch):
    fake = FakeApi([
        {"id": "OLD-1", "state": "done", "archived": 0, "version": 3,
         "completed_at": OLD, "parent_id": None},
    ])
    monkeypatch.setattr(probe.core, "api", fake)
    probe.core.execute("http://x", {"agent": "probe", "action": "archive-sweep", "days": 0})
    assert fake.patches == []
