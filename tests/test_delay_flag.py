"""SRM1: 지연 카드 표기 — todo 24h+ 미수령 → delayed 필드 + CLI/보드 노란불.

사람이 backlog로 내리는 판단용 표시(자동 전환 없음). todo↔backlog 전이 시점은
서버가 todo_since로 기록(기존 행은 created_at 백필).
"""
import sqlite3
from datetime import datetime, timedelta, timezone

from test_cli_display import cli, new  # noqa: F401


def _backdate_todo_since(db, iid, hours):
    ts = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S%z")
    con = sqlite3.connect(db)
    con.execute("UPDATE issues SET todo_since=? WHERE id=?", (ts, iid))
    con.commit()
    con.close()


def test_생성_todo는_todo_since_기록_미지연(cli):
    run, url, api, db = cli
    iid = new(run)
    d = api(f"/issues/{iid}")
    assert d["todo_since"]
    assert d["delayed"] is False


def test_backlog_이동시_클리어_재진입시_재기록(cli):
    run, url, api, db = cli
    iid = new(run)
    api(f"/issues/{iid}", "PATCH", {"state": "backlog"})
    d = api(f"/issues/{iid}")
    assert d["todo_since"] is None and d["delayed"] is False
    api(f"/issues/{iid}", "PATCH", {"state": "todo"})
    d = api(f"/issues/{iid}")
    assert d["todo_since"] and d["delayed"] is False


def test_claim하면_todo_since_클리어(cli):
    run, url, api, db = cli
    iid = new(run)
    run("claim", iid)
    d = api(f"/issues/{iid}")
    assert d["state"] == "in_progress"
    assert d["todo_since"] is None and d["delayed"] is False


def test_24h_경과_todo만_delayed(cli):
    run, url, api, db = cli
    iid = new(run)
    _backdate_todo_since(db, iid, 25)
    assert api(f"/issues/{iid}")["delayed"] is True
    _backdate_todo_since(db, iid, 23)
    assert api(f"/issues/{iid}")["delayed"] is False


def test_list_지연_노란불_지연카드만(cli):
    run, url, api, db = cli
    fresh = new(run)
    stale = new(run)
    _backdate_todo_since(db, stale, 25)
    out = run("list", "todo").stdout
    lines = {l.split()[0]: l for l in out.splitlines()}
    assert "🟡지연" in lines[stale]
    assert "🟡지연" not in lines[fresh]


def test_기존_todo_행은_created_at_백필(cli):
    run, url, api, db = cli
    iid = new(run)
    con = sqlite3.connect(db)
    con.execute("UPDATE issues SET todo_since=NULL WHERE id=?", (iid,))
    con.commit()
    con.close()
    d = api(f"/issues/{iid}")
    assert d["todo_since"]  # 다음 connect에서 백필
    assert d["delayed"] is False  # 새 카드라 24h 미만
