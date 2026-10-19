"""NB5T(M58SPX0G-NB5T): build_graph 투영 — 그래프 질의==문자열 파싱 동등성,
결정적 투영, decide golden 9종 일치(하드 조건)."""
import probe.core as core
from ontology.build import build_graph, parse_comment_pr_repos, parse_requested
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
    assert g.requested_reviewers(issue["id"], 9, "abc12345") == \
        core._requested_reviewers(issue, 9, "abc12345") == {"agy", "cursor-review"}
    assert g.requested_reviewers(issue["id"], 9, "abc12346") == set()  # 다른 head
    assert g.requested_reviewers(issue["id"], 8, "abc12345") == set()  # 다른 PR


def test_graph_deterministic():
    """동일 스냅샷 → 동일 그래프(결정적 투영, RD §4.1)."""
    s1 = build_graph({"issues": [_rich_issue()]})
    s2 = build_graph({"issues": [_rich_issue()]})
    assert s1._edges == s2._edges
    assert s1._card_pr_urls == s2._card_pr_urls


def test_machine_comments_not_projected_as_requests():
    issue = _issue(comments=[
        {"id": 1, "author": "tt-server", "body": "[review-req #5/deadbeef] → agy"},
        {"id": 2, "author": "runner@m2max", "body": "[review-req #5/deadbeef] → claude"},
    ])
    g = build_graph({"issues": [issue]})
    assert g.requested_reviewers(issue["id"], 5, "deadbeef") == set()


def test_decide_linked_pr_edges():
    """Card —linkedTo→ PR — pr_card_id 조인, provenance에 full sha."""
    pr = {"number": 9, "repo": "plainOldCode/think-tank", "state": "OPEN",
          "branch": "tt/M58SPX0G-NB5T-1", "head_sha": "0123456789abcdef",
          "title": "M58SPX0G-NB5T: 투영"}
    s = snap({"id": "M58SPX0G-NB5T", "state": "review", "comments": [],
              "title": "x", "execution_attempt": 1})
    s["prs"] = [pr]
    ctx = core._DecideCtx(s)
    assert ctx.graph.has_edge("M58SPX0G-NB5T", "linkedTo",
                              "plainOldCode/think-tank#9")
    edges = [e for e in ctx.graph.edges() if e[1] == "linkedTo"]
    assert edges[0][3]["sha"] == "0123456789abcdef"  # full sha provenance


def test_parse_helpers_keep_contracts():
    """비-decide 호출자용 위임 함수도 정본 파서와 동일 결과."""
    issue = _rich_issue()
    assert parse_comment_pr_repos(issue["comments"]) == \
        [(  # 등장 순 중복 제거 — id 2의 URL만 코멘트 PR URL에 해당
            "plainOldCode/think-tank", 9)]
    assert parse_requested(issue["comments"]) == {
        (9, "abc12345"): {"agy", "cursor-review"}}
