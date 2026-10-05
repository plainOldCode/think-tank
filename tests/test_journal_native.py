"""일지 체크인 서버 내장화 (M4580RJK-C9B0): 외부 크론/스크립트 제거, probe가 tt native로 수행.

- journal_tick(url, hours=None, now=None): 등록 에이전트 순회 — 유휴(본인 활성 lease 0)만 기록
- 일지 카드(라벨 '일지', 제목 '일지: <agent>') 없으면 claim 없이 생성(new→PATCH in_progress+assignee)
- 체크인 코멘트 author=에이전트, 3블록(한 일/할 일/지시사항)
- 주기 게이트: 일지 카드의 본인 마지막 체크인이 hours 이내면 스킵 — 별도 상태 저장 없이 코멘트에서 유도
- TT_JOURNAL_HOURS=0 → 비활성, dry-run 미실행, enabled=False 에이전트 스킵
"""
import threading
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import probe.core as core
from app import create_app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("TT_JOURNAL_HOURS", raising=False)
    app = create_app(str(tmp_path / "p.db"))
    with TestClient(app) as c:
        yield c


def _mk_agent(c, name, **kw):
    r = c.post("/agents", json={"name": name, "base_url": "http://x", **kw})
    assert r.status_code == 201, r.text


def _patch_api(c, monkeypatch):
    def fake_api(url, path, method="GET", body=None):
        r = c.request(method, path, json=body)
        if r.status_code >= 400:
            raise RuntimeError(f"{method} {path} → {r.status_code}: {r.text[:200]}")
        return r.json()

    monkeypatch.setattr(core, "api", fake_api)


def _journal(client):
    return next(i for i in client.get("/issues?limit=100&with_comments=1").json() if "일지" in i["labels"])


def test_issues_리스트_with_comments_옵션(client):
    iid = client.post("/issues", json={"title": "x"}).json()["id"]
    client.post(f"/issues/{iid}/comments", json={"author": "a", "body": "b"})
    row = next(r for r in client.get("/issues?with_comments=1").json() if r["id"] == iid)
    assert row["comments"] and row["comments"][0]["body"] == "b"
    row2 = next(r for r in client.get("/issues").json() if r["id"] == iid)
    assert "comments" not in row2, "기본은 comments 미포함(하위호환)"


def test_유휴_에이전트_일지_신설_및_체크인(client, monkeypatch):
    _mk_agent(client, "codex")
    _patch_api(client, monkeypatch)
    acts = core.journal_tick("http://self")
    j = _journal(client)
    assert j["title"] == "일지: codex"
    assert j["state"] == "in_progress" and j["assignee"] == "codex"
    assert not j["lease_by"], "claim 없이 생성 — 본인 lease 점유 금지(유휴 정의 보존)"
    cs = j["comments"]
    assert len(cs) == 1 and cs[0]["author"] == "codex"
    for blk in ("한 일", "할 일", "지시사항"):
        assert blk in cs[0]["body"]
    assert {a["action"] for a in acts} == {"journal-new", "journal-checkin"}


def test_활성_lease_보유_에이전트_스킵(client, monkeypatch):
    _mk_agent(client, "codex")
    iid = client.post("/issues", json={"title": "작업", "labels": ["auto"]}).json()["id"]
    assert client.post(f"/issues/{iid}/claim", json={"agent": "codex"}).status_code in (200, 201)
    _patch_api(client, monkeypatch)
    core.journal_tick("http://self")
    assert not [i for i in client.get("/issues?limit=100").json() if "일지" in i["labels"]]


def test_주기게이트_hours이내_재체크인_금지(client, monkeypatch):
    _mk_agent(client, "codex")
    _patch_api(client, monkeypatch)
    core.journal_tick("http://self")
    core.journal_tick("http://self")
    assert len(_journal(client)["comments"]) == 1


def test_주기게이트_경과후_재체크인(client, monkeypatch):
    _mk_agent(client, "codex")
    _patch_api(client, monkeypatch)
    core.journal_tick("http://self")
    later = datetime.now(timezone.utc).astimezone() + timedelta(hours=7)
    core.journal_tick("http://self", now=later)
    assert len(_journal(client)["comments"]) == 2


def test_비활성_에이전트_스킵(client, monkeypatch):
    _mk_agent(client, "agy", enabled=False)
    _patch_api(client, monkeypatch)
    core.journal_tick("http://self")
    assert not [i for i in client.get("/issues?limit=100").json() if "일지" in i["labels"]]


def test_TT_JOURNAL_HOURS_0이면_비활성(client, tmp_path, monkeypatch):
    monkeypatch.setenv("TT_JOURNAL_HOURS", "0")
    _mk_agent(client, "codex")
    _patch_api(client, monkeypatch)
    core.journal_tick("http://self")
    assert not [i for i in client.get("/issues?limit=100").json() if "일지" in i["labels"]]


def test_지시사항_체크인_본문_수집(client, monkeypatch):
    _mk_agent(client, "codex")
    _patch_api(client, monkeypatch)
    core.journal_tick("http://self")
    jid = _journal(client)["id"]
    client.post(f"/issues/{jid}/comments", json={"author": "tp-13", "body": "다음 스프린트 우선순위 정리해줘"})
    later = datetime.now(timezone.utc).astimezone() + timedelta(hours=7)
    core.journal_tick("http://self", now=later)
    body = client.get(f"/issues/{jid}").json()["comments"][-1]["body"]
    assert "지시사항" in body and "다음 스프린트 우선순위" in body


def test_한일_타카드_활동_수집(client, monkeypatch):
    _mk_agent(client, "codex")
    iid = client.post("/issues", json={"title": "구현 카드", "labels": []}).json()["id"]
    client.post(f"/issues/{iid}/comments", json={"author": "codex", "body": "구현 완료 보고"})
    _patch_api(client, monkeypatch)
    core.journal_tick("http://self")
    body = _journal(client)["comments"][0]["body"]
    assert "한 일" in body and "구현 카드" in body


def test_run_once_비드라이에서만_저널_틱(monkeypatch):
    calls = []
    monkeypatch.setattr(core, "journal_tick", lambda url, **k: calls.append(url))
    monkeypatch.setattr(core, "snapshot",
                        lambda url: {"auto": False, "now": "", "agents": [], "issues": []})
    monkeypatch.setattr(core, "collect_prs", lambda repos: [])
    monkeypatch.setattr(core, "decide", lambda snap: [])
    core.run_once("http://self", dry=True)
    assert not calls, "dry-run에서는 기록하지 않는다"
    core.run_once("http://self")
    assert calls == ["http://self"]


def _seed_auto_card(client):
    return client.post("/issues", json={"title": "자동작업", "labels": ["auto"]}).json()["id"]


def _decide_snap(client):
    import db as dbmod
    issues = client.get("/issues?limit=500&with_comments=1").json()
    for i in issues:
        i.setdefault("dispatches", 0)
    return {"auto": True, "now": dbmod.now(),
            "agents": client.get("/agents").json(), "issues": issues}


def test_issues_lease_by_필터(client):
    iid = client.post("/issues", json={"title": "x"}).json()["id"]
    assert client.post(f"/issues/{iid}/claim", json={"agent": "codex"}).status_code in (200, 201)
    assert [r["id"] for r in client.get("/issues?lease_by=codex").json()] == [iid]
    assert client.get("/issues?lease_by=없는에이전트").json() == []


def test_일지_카드가_자동배정_막지_않음(client, monkeypatch):
    """codex F1 회귀: 일지 카드(in_progress+assignee, lease 없음)는 유휴 판정에서 제외.

    유휴 규약 = 본인 활성 lease 0개 — 일지 스레드는 작업이 아니다(decide.idle과 규약 정렬).
    """
    _mk_agent(client, "codex")
    _seed_auto_card(client)
    _patch_api(client, monkeypatch)
    core.journal_tick("http://self")
    acts = core.decide(_decide_snap(client))
    assert any(a["action"] == "work" for a in acts), "일지 카드가 자동 배정을 죽이면 안 됨"


def _deterministic_clock(monkeypatch):
    """created_at 초 단위 동률 제거 — 삽입마다 1초씩 증가한 시각을 부여."""
    import db as dbmod
    import itertools
    tick = itertools.count()

    def fake_now():
        n = next(tick)
        return f"2030-01-01T00:{n // 60:02d}:{n % 60:02d}+0900"

    def fake_future(hours):
        # created_at 카운터와 독립 — 2030-06-01 기준(00:xx created_at 범위보다 확실히 미래)
        return f"2030-06-01T{hours:02d}:00:00+0900"

    monkeypatch.setattr(dbmod, "now", fake_now)
    monkeypatch.setattr(dbmod, "future", fake_future)


def test_500건_경계_밖_기존_일지_중복_신설_방지(client, monkeypatch):
    """codex F2 회귀: created_at DESC limit=500 밖의 기존 일지를 재신설하지 않는다."""
    _deterministic_clock(monkeypatch)
    _mk_agent(client, "codex")
    _patch_api(client, monkeypatch)
    core.journal_tick("http://self")
    for n in range(500):
        client.post("/issues", json={"title": f"채움{n}"})
    acts = core.journal_tick("http://self")
    journals = [i for i in client.get("/issues?limit=1000").json() if "일지" in i["labels"]]
    assert len(journals) == 1, "일지 카드 중복 신설 금지"
    assert not any(a["action"] == "journal-new" for a in acts)


def test_500건_경계_밖_활성_lease_바쁨_인정(client, monkeypatch):
    """codex F2 회귀: 목록 밖의 활성 lease도 바쁨으로 인정 — 체크인 스킵."""
    _deterministic_clock(monkeypatch)
    _mk_agent(client, "codex")
    iid = client.post("/issues", json={"title": "작업"}).json()["id"]
    client.post(f"/issues/{iid}/claim", json={"agent": "codex"})
    for n in range(500):
        client.post("/issues", json={"title": f"채움{n}"})
    _patch_api(client, monkeypatch)
    acts = core.journal_tick("http://self")
    assert not any(a["action"] in ("journal-new", "journal-checkin") for a in acts)


def _shifted_clock(monkeypatch):
    """제어 가능한 서버 시계 — db.now/db.future를 테스트가 임의로 전진."""
    import db as dbmod
    box = {"t": datetime.now().astimezone().replace(microsecond=0)}

    def fake_now():
        return box["t"].strftime("%Y-%m-%dT%H:%M:%S%z")

    def fake_future(hours):
        return (box["t"] + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S%z")

    monkeypatch.setattr(dbmod, "now", fake_now)
    monkeypatch.setattr(dbmod, "future", fake_future)
    return box


def test_만료_lease_10건_경계에서_활성_lease_인정(client, monkeypatch):
    """codex F2 잔여: LIMIT 전에 만료를 걸러야 한다 — 만료 lease로 슬롯을 채우면
    구형 활성 lease가 목록 밖으로 밀려나 유휴 오판했다(개정 전)."""
    box = _shifted_clock(monkeypatch)
    _patch_api(client, monkeypatch)
    _mk_agent(client, "codex")
    active = client.post("/issues", json={"title": "활성작업"}).json()["id"]
    assert client.post(f"/issues/{active}/claim", json={"agent": "codex", "hours": 6}).status_code in (200, 201)
    for n in range(10):
        box["t"] += timedelta(hours=2)  # 직전 filler의 lease가 만료되게
        f = client.post("/issues", json={"title": f"만료{n}"}).json()["id"]
        assert client.post(f"/issues/{f}/claim", json={"agent": "codex", "hours": 1}).status_code in (200, 201)
        client.post(f"/issues/{active}/lease", json={"agent": "codex", "hours": 6})  # 활성 유지
    box["t"] += timedelta(hours=2)
    acts = core.journal_tick("http://self", now=box["t"])  # 틱 시계도 테스트 시계와 맞춘다
    assert not any(a["action"] in ("journal-new", "journal-checkin") for a in acts), \
        "활성 lease가 목록 밖으로 밀려도 바쁨으로 인정해야 함"


def test_보관된_카드의_활성_lease_바쁨_인정(client, monkeypatch):
    """codex F2 잔여: archived 카드의 lease도 활성다 — 기본 archived=no 조회로는 누락."""
    _shifted_clock(monkeypatch)
    _patch_api(client, monkeypatch)
    _mk_agent(client, "codex")
    iid = client.post("/issues", json={"title": "보관작업"}).json()["id"]
    assert client.post(f"/issues/{iid}/claim", json={"agent": "codex", "hours": 6}).status_code in (200, 201)
    assert client.patch(f"/issues/{iid}", json={"archived": True, "version": None}).status_code in (200, 409)
    acts = core.journal_tick("http://self")
    assert not any(a["action"] in ("journal-new", "journal-checkin") for a in acts), \
        "보관된 카드의 활성 lease도 바쁨으로 인정해야 함"


def test_active_lease_단독_사용도_만료_제외(client, monkeypatch):
    """codex F3: active_lease는 lease_by 없이 단독으로도 만료·무lease 카드를 제외해야 한다."""
    box = _shifted_clock(monkeypatch)
    a = client.post("/issues", json={"title": "활성"}).json()["id"]
    assert client.post(f"/issues/{a}/claim", json={"agent": "codex", "hours": 6}).status_code in (200, 201)
    e = client.post("/issues", json={"title": "만료"}).json()["id"]
    assert client.post(f"/issues/{e}/claim", json={"agent": "codex", "hours": 1}).status_code in (200, 201)
    client.post("/issues", json={"title": "무lease"})
    box["t"] += timedelta(hours=2)  # 만료 카드의 lease가 확실히 지나게
    ids = {r["title"] for r in client.get("/issues?active_lease=1&archived=all").json()}
    assert ids == {"활성"}, f"활성만 반환해야 함: {ids}"
    assert {r["title"] for r in client.get("/issues?lease_by=codex&active_lease=1&archived=all").json()} == {"활성"}
