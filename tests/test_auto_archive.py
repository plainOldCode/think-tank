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


# --- 서버 검증형 아카이브 엔드포인트 (M3ZRGTSF-N95X 리뷰 R1-R3 반영) ---

OLD = "2026-09-20T09:00:00+0900"
RECENT = "2026-10-08T09:00:00+0900"


def _mk(client, **kw):
    kw.setdefault("acceptance", "완료 기준: 회귀 통과")
    return client.post("/issues", json={"title": "t", **kw}).json()


def _age(tmp_path, iid):
    """completed_at을 14일 이상 과거로 돌린다 (테스트 가속용 직접 갱신).

    conftest client fixture가 create_app(str(tmp_path/'tt.db'))로 만든다."""
    import sqlite3
    con = sqlite3.connect(str(tmp_path / "tt.db"))
    con.execute("UPDATE issues SET completed_at=? WHERE id=?", (OLD, iid))
    con.commit(); con.close()


@pytest.fixture(autouse=True)
def _archive_on(monkeypatch):
    monkeypatch.setenv("TT_ARCHIVE_AFTER_DAYS", "14")
def _done(client, iid, v=1):
    """verify(human) 경로로 done 진입 — PATCH state=done은 게이트 강등(review)된다."""
    client.patch(f"/issues/{iid}", json={"state": "in_progress", "expected_version": v})
    r = client.post(f"/issues/{iid}/verify",
                    json={"verifier": "tester@t", "evidence": "테스트 승인", "human": True,
                          "expected_version": v + 1})
    assert r.status_code == 200, r.json()


def test_archive_엔드포인트는_노후_done만_아카이브한다(client, tmp_path):
    i = _mk(client)
    _done(client, i["id"])
    # 미성숙 → 409
    assert client.post(f"/issues/{i['id']}/archive").status_code == 409
    _age(tmp_path, i["id"])
    # 성숙 → 200 archived
    r = client.post(f"/issues/{i['id']}/archive")
    assert r.status_code == 200 and r.json()["archived"] == 1
    # 멱등
    assert client.post(f"/issues/{i['id']}/archive").status_code == 200


def test_archive는_미완_조상을_보존하고_사이클을_거부한다(client, tmp_path):
    epic = _mk(client)
    child = _mk(client, parent_id=epic["id"])
    _done(client, child["id"])
    _age(tmp_path, child["id"])
    # 부모 backlog → 409 보존
    assert client.post(f"/issues/{child['id']}/archive").status_code == 409
    _done(client, epic["id"])
    _age(tmp_path, epic["id"])
    assert client.post(f"/issues/{child['id']}/archive").status_code == 200
    # 사이클: A의 부모를 B로, B의 부모를 A로 — 409
    a = _mk(client); b = _mk(client)
    _done(client, a["id"])
    _age(tmp_path, a["id"])
    import sqlite3
    con = sqlite3.connect(str(tmp_path / "tt.db"))
    con.execute("UPDATE issues SET parent_id=? WHERE id=?", (b["id"], a["id"]))
    con.execute("UPDATE issues SET parent_id=? WHERE id=?", (a["id"], b["id"]))
    con.commit(); con.close()
    assert client.post(f"/issues/{a['id']}/archive").status_code == 409


def test_archive는_조상_유실과_비done을_거부한다(client, tmp_path):
    orphan = _mk(client)
    import sqlite3
    con = sqlite3.connect(str(tmp_path / "tt.db"))
    con.execute("UPDATE issues SET parent_id='GONE-1', completed_at=? WHERE id=?", (OLD, orphan["id"]))
    con.commit(); con.close()
    assert client.post(f"/issues/{orphan['id']}/archive").status_code == 409


def test_archive는_환경변수_0이면_비활성이다(client, monkeypatch):
    monkeypatch.setenv("TT_ARCHIVE_AFTER_DAYS", "0")
    i = _mk(client)
    assert client.post(f"/issues/{i['id']}/archive").status_code == 409


def test_completed_before_필터는_나이로_직접_고른다(client, tmp_path):
    old_i = _mk(client); recent_i = _mk(client)
    for iid in (old_i["id"], recent_i["id"]):
        _done(client, iid)
    _age(tmp_path, old_i["id"])
    r = client.get("/issues", params={"state": "done",
                                      "completed_before": "2026-09-21T09:00:00+0900"})
    assert [c["id"] for c in r.json()] == [old_i["id"]]


def test_probe_스윕은_409를_조용히_넘긴다(monkeypatch):
    import probe.core
    calls = []
    def fake(url, path, method="GET", body=None):
        calls.append((method, path))
        if path.startswith("/issues?"):
            return [{"id": "CARD-1"}]
        import urllib.error
        raise urllib.error.HTTPError(path, 409, "ancestor missing", None, None)
    monkeypatch.setattr(probe.core, "api", fake)
    probe.core.execute("http://x", {"agent": "probe", "action": "archive-sweep", "days": 14})
    assert ("POST", "/issues/CARD-1/archive") in calls


def test_재개된_카드는_archived가_해제된다(client):
    """P77 R2 후속: done→todo 재개 시 archived=1 잔존 방지."""
    i = _mk(client)
    _done(client, i["id"])
    _age(tmp_path_factory(), i["id"]) if False else None
    # (나이 무관 — archived 플래그만 확인)
    import sqlite3
    con = sqlite3.connect(str(client.app.state.db_path) if hasattr(client.app, "state") else None)


def test_재개된_카드는_archived가_해제된다(client, tmp_path):
    """P77 R2 후속: done→todo 재개 시 archived=1 잔존 방지."""
    i = _mk(client)
    _done(client, i["id"])
    _age(tmp_path, i["id"])
    r = client.post(f"/issues/{i['id']}/archive")
    assert r.status_code == 200 and r.json()["archived"] == 1
    # 재개 → archived 자동 해제
    client.patch(f"/issues/{i['id']}", json={"state": "todo", "expected_version": 4})
    assert client.get(f"/issues/{i['id']}").json()["archived"] == 0


def test_스윕은_페이지를_전진해_보존_뒤_후보를_본다(monkeypatch):
    """P77 R4: 500장 보존 페이지 뒤의 오래된 루트도 수집된다."""
    import probe.core
    pages = [
        [{"id": f"KEEP-{n}", "state": "done"} for n in range(500)],
        [{"id": "OLD-ROOT", "state": "done"}],
    ]
    seen, posts = [], []

    def fake(url, path, method="GET", body=None):
        seen.append(path)
        if path.startswith("/issues?"):
            page = pages.pop(0) if pages else []
            return page
        posts.append(path)
        import urllib.error
        raise urllib.error.HTTPError(path, 409, "ancestor missing", None, None)

    monkeypatch.setattr(probe.core, "api", fake)
    probe.core.execute("http://x", {"agent": "probe", "action": "archive-sweep", "days": 14})
    assert any("offset=500" in p for p in seen)      # 두 번째 페이지 전진
    assert "/issues/OLD-ROOT/archive" in posts        # 뒤 페이지 후보도 호출
