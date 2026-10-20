"""Snapshot pagination must reach old cards beyond the API page limit (FR1S)."""
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

import probe.core as core


def _seed(client, count):
    # Bulk seed the isolated SQLite database; reads still use the real /issues API.
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    ids = [f"M{n:07d}-TEST" for n in range(count)]
    with client.app.state.ctx.con() as con:
        con.executemany(
            "INSERT INTO issues (id,title,state,created_at,updated_at) VALUES (?,?,?,?,?)",
            [(iid, iid, "done", (start + timedelta(seconds=n)).isoformat(),
              start.isoformat()) for n, iid in enumerate(ids)],
        )
        con.commit()
    return ids


@pytest.fixture
def snapshot_api(client, monkeypatch):
    calls = []

    def install(page_cap=1000, fail_offset=None):
        def api(url, path, method="GET", body=None):
            calls.append((method, path))
            parts = urlsplit(path)
            if parts.path == "/issues":
                query = parse_qs(parts.query)
                offset = int(query.get("offset", ["0"])[0])
                if offset == fail_offset:
                    raise RuntimeError("next issue page unavailable")
                # Also exercise deployments that cap responses at 500 rows.
                query["limit"] = [min(int(query.get("limit", ["200"])[0]), page_cap)]
                path = "/issues?" + urlencode(query, doseq=True)
            response = client.request(method, path, json=body)
            assert response.status_code == 200, response.text
            return response.json()

        monkeypatch.setattr(core, "api", api)
        return calls

    return install


@pytest.mark.parametrize("page_cap", [500, 1000])
@pytest.mark.parametrize("count", [0, 499, 500, 501, 999, 1000, 1001, 2000])
def test_snapshot_reads_every_issue_at_page_boundaries(client, snapshot_api, page_cap, count):
    ids = _seed(client, count)
    calls = snapshot_api(page_cap)

    snap = core.snapshot("http://test")

    assert [i["id"] for i in snap["issues"]] == ids[::-1]
    queries = [parse_qs(urlsplit(path).query) for _, path in calls
               if urlsplit(path).path == "/issues"]
    assert all(q["limit"] == ["1000"] for q in queries)
    offsets = [int(q.get("offset", ["0"])[0]) for q in queries]
    assert offsets == list(range(0, count, page_cap)) + [count]
    assert calls.count(("GET", "/agents")) == 1


def test_snapshot_enriches_and_decides_cards_on_later_pages(client, snapshot_api, monkeypatch):
    ids = _seed(client, 1004)
    review, blocked, todo = ids[:3]  # All three sit beyond the first 1000 rows.
    with client.app.state.ctx.con() as con:
        con.execute("UPDATE issues SET state='review', execution_attempt=1, work_contract=? WHERE id=?",
                    ('{"version":"v2"}', review))
        con.execute("UPDATE issues SET state='blocked', waiting_for='dependency', blocked_detail=? WHERE id=?",
                    (f"dependency: {ids[-1]}", blocked))
        con.execute("UPDATE issues SET state='todo', labels='auto', brief_verify='pytest -q' WHERE id=?", (todo,))
        con.execute("INSERT INTO comments (issue_id,author,body,ts) VALUES (?,?,?,?)",
                    (review, "tester", "review context", "2026-01-01T00:00:00"))
        con.commit()
    assert client.post("/agents", json={"name": "worker", "base_url": "http://127.0.0.1:1",
                                        "receive_mode": "hook"}).status_code == 201
    calls = snapshot_api()
    monkeypatch.setenv("TT_AUTO_DISPATCH", "1")

    snap = core.snapshot("http://test")
    cards = {i["id"]: i for i in snap["issues"]}

    assert cards[review]["comments"][0]["body"] == "review context"
    assert cards[blocked]["release_ready"] is True
    assert cards[todo]["dispatches"] == 0
    assert cards[todo]["busy_agents"] == []
    assert ("GET", f"/issues/{review}") in calls
    assert ("GET", f"/issues/{blocked}/why-blocked") in calls
    assert ("GET", f"/issues/{todo}/dispatches") in calls
    acts = core.decide(snap)
    assert any(a["issue"] == blocked and a["action"] == "resume" for a in acts)
    assert snap["probe_skips"][blocked] == "state:blocked"
    # A release-ready dependency takes precedence; without it work/review proceed.
    snap["issues"] = [i for i in snap["issues"] if i["id"] != blocked]
    acts = core.decide(snap)
    assert not any(a["issue"] == review and a["action"] == "review-note" for a in acts)
    assert any(a["issue"] == todo and a["action"] == "work" for a in acts)


def test_snapshot_does_not_return_a_partial_scan_on_page_failure(client, snapshot_api):
    _seed(client, 1001)
    calls = snapshot_api(fail_offset=1000)

    with pytest.raises(RuntimeError, match="next issue page unavailable"):
        core.snapshot("http://test")

    assert ("GET", "/agents") not in calls
