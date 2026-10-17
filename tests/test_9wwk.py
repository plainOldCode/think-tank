"""9WWK: 판정 파서 단일 소스(server/verdicts.py) — rejections 집계·probe·지표가
같은 규칙을 쓴다. 운영 실측: 본문 LIKE 집계가 runner 에코·보강 메모까지 반려로
세어 27%가 비판정이었다.
"""
from conftest import mk


def _comments(client, issue_id):
    return client.get(f"/issues/{issue_id}").json()["comments"]


def test_기계_author의_에코는_판정이_아니다():
    from server.verdicts import verdict_of
    body = ("runner: dispatch#561 done exit=0 session=tt-codex-561\n"
            "1. ... 결론은 review: request-changes 이다")
    assert verdict_of("runner@m2max", body) is None
    assert verdict_of("probe", "review: request-changes\nTT#X@a1/s") is None
    assert verdict_of("tt-server", "review: request-changes") is None
    assert verdict_of("board", "review: request-changes") is None


def test_본문_중간_인용과_메모는_판정이_아니다():
    from server.verdicts import first_line_verdict, verdict_of
    assert verdict_of("agy", "보강 메모 — 이전 판정 review: request-changes 참고") is None
    assert verdict_of("codex", '지시문 인용:\n"review: request-changes"\n위와 같다') is None
    assert first_line_verdict("review: request-changes") == "request-changes"


def test_실제_판정_댓글은_집계된다():
    from server.verdicts import verdict_of
    assert verdict_of("agy", "review: request-changes\nTT#X@a1/s\n근거") == "request-changes"
    assert verdict_of("codex", "review: approve\nTT#X@a1/s") == "approve"
    # 대소문자 규약 — 정확한 소문자만
    assert verdict_of("agy", "Review: request-changes") is None


def test_세_소비처가_같은_입력에_같은_판정을_낸다():
    import config
    import probe.core as core
    from server.verdicts import first_line_verdict, verdict_of
    body = "review: request-changes\nPR#1@abcd1234\n회귀 결함"
    assert verdict_of("agy", body) == first_line_verdict(body)
    c = [{"author": "agy", "body": body}]
    # probe review_verdict의 판정 인정 규칙도 같은 함수를 쓴다
    assert core.review_verdict(c, "agy", 1, "abcd1234") == verdict_of("agy", body)
    assert config is not None  # 같은 트리 — 집합 계약은 test_3a0b 참조


def test_rejections_summary는_첫_줄_판정만_센다(client):
    from server.verdicts import is_machine_author
    assert not is_machine_author("agy")
    issue = mk(client, title="9wwk 집계")
    iid = issue["id"]
    client.post(f"/issues/{iid}/comments", json={"author": "agy",
                "body": "review: request-changes\nTT#9WWK@a1/s\n테스트 회귀 결함"})
    client.post(f"/issues/{iid}/comments", json={"author": "runner@m2max",
                "body": "dispatch#9 done exit=0\n... review: request-changes (에코)"})
    client.post(f"/issues/{iid}/comments", json={"author": "agy",
                "body": "메모: 이전 review: request-changes 와 같은 맥락"})

    r = client.get("/rejections/summary", params={"days": 30})
    assert r.status_code == 200, r.text
    buckets = {b["bucket"]: b["count"] for b in r.json()["buckets"]}
    assert sum(buckets.values()) == 1  # 에코·메모 제외 — 실제 판정 1건
    assert buckets.get("test") == 1
