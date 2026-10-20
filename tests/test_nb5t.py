"""NB5T(M58SPX0G-NB5T): build_graph 투영 — 그래프 질의==문자열 파싱 동등성, 결정적 투영.

P5(M5BH91TA-S28F) — 레거시 [review-req] 질의(requested_reviewers)와 edge 저장소
(has_edge·edges·link_pr)는 prod 리더가 없어 제거됐다. 유일 질의는 comment_pr_repos.
"""
import probe.core as core
from ontology.build import build_graph, parse_comment_pr_repos
from test_tt_lifecycle import snap


def _issue(iid="M58SPX0G-NB5T", comments=(), body="", labels=()):
    return {"id": iid, "state": "review", "comments": list(comments),
            "body": body, "labels": list(labels), "title": "t",
            "execution_attempt": 1}


def _rich_issue():
    comments = [
        {"id": 1, "author": "probe",
         "body": "[review-req #9/abc12345] PR#9 CI green — 요청(계열 codex) → agy"},
        {"id": 2, "author": "codex",
         "body": "관련 PR: https://github.com/plainOldCode/think-tank/pull/9 도 참고"},
        {"id": 3, "author": "probe",
         "body": "[review-req #9/abc12345] 재요청 → cursor-review."},
        {"id": 4, "author": "worker", "body": "https://github.com/plainOldCode/think-tank/pull/9 중복 URL"},
    ]
    return _issue(comments=comments)


def test_graph_queries_equal_string_parsing():
    """수용기준 3 — 같은 스냅샷에서 그래프 질의와 구 문자열 파싱이 동일하다."""
    issue = _rich_issue()
    g = build_graph({"issues": [issue]})
    assert g.comment_pr_repos(issue["id"]) == core._comment_pr_repos(issue)
    # 레거시 [review-req] 마커는 투영 대상 아님(P5) — prod 질의에 영향 없음
    assert g.comment_pr_repos(issue["id"]) == [("plainOldCode/think-tank", 9)]


def test_graph_deterministic():
    """동일 스냅샷 → 동일 그래프(결정적 투영, RD §4.1)."""
    s1 = build_graph({"issues": [_rich_issue()]})
    s2 = build_graph({"issues": [_rich_issue()]})
    assert s1._card_pr_urls == s2._card_pr_urls


def test_parse_helpers_keep_contracts():
    """비-decide 호출자용 위임 함수도 정본 파서와 동일 결과."""
    issue = _rich_issue()
    assert parse_comment_pr_repos(issue["comments"]) == \
        [(  # 등장 순 중복 제거 — id 2의 URL만 코멘트 PR URL에 해당
            "plainOldCode/think-tank", 9)]
