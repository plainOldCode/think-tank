"""probe 관계 그래프 투영 (NB5T, RD §4.1·§4.4) — read-only, DB 쓰기 없음.

스냅샷의 코멘트·PR을 한 번 투영해 decide의 문자열 재파싱을 질의로 바꾼다.
노드는 카탈로그 통과분만(unknown은 기록만), edge는 provenance(comment_id·
author·attempt·repo·pr·sha)를 갖는다. 동일 스냅샷 → 동일 그래프(결정적).
이 모듈은 core를 임포트하지 않는다(단방향 — core가 이 모듈을 소비).
"""
import re

PR_URL = re.compile(r"https://github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)")
PR_SHA = re.compile(r"#(\d+)/([0-9a-fA-F]{8})")


def parse_comment_pr_repos(comments):
    """카드 코멘트의 github PR URL → [(owner/repo, pr_no)] — 등장 순 중복 제거.

    core._comment_pr_repos의 정본 구현(동일 시맨틱) — decide는 그래프 질의로,
    비-decide 호출자는 core 위임 함수로 쓴다."""
    seen = []
    for c in comments or []:
        for m in PR_URL.finditer(c.get("body") or ""):
            t = (m.group(1), int(m.group(2)))
            if t not in seen:
                seen.append(t)
    return seen


def parse_requested(comments):
    """[review-req #pr/sha8] 마커의 '→ <agent>' → {(pr_no, sha8): set(agent)}.

    core._requested_reviewers의 정본 구현 — probe 코멘트만, (pr, sha8) 단위."""
    table = {}
    for c in comments or []:
        if c.get("author") != "probe":
            continue
        body = c.get("body") or ""
        m = re.search(r"→\s*(\S+)", body)
        if not m:
            continue
        agent = m.group(1).rstrip(".")
        for pm in PR_SHA.finditer(body):
            table.setdefault((int(pm.group(1)), pm.group(2).lower()), set()).add(agent)
    return table


class Graph:
    """카드 중심 관계 질의 — decide의 재파싱 지점을 대체한다(NB5T 단계 치환)."""

    def __init__(self):
        self._edges = []            # (s, p, o, prov)
        self._card_pr_urls = {}     # card_id → [(repo, pr_no)]
        self._card_review_req = {}  # card_id → {(pr_no, sha8): set(agent)}

    def add_edge(self, s, p, o, **prov):
        self._edges.append((s, p, o, prov))

    def has_edge(self, s, p, o):
        return any(e[0] == s and e[1] == p and e[2] == o for e in self._edges)

    def edges(self):
        return list(self._edges)

    def set_comment_pr_repos(self, card_id, pairs):
        self._card_pr_urls[card_id] = list(pairs)

    def comment_pr_repos(self, card_id):
        """Card —commentUrl→ PR — 코멘트 등장 순 중복 제거."""
        return list(self._card_pr_urls.get(card_id) or [])

    def set_requested(self, card_id, table):
        self._card_review_req[card_id] = {k: set(v) for k, v in table.items()}

    def requested_reviewers(self, card_id, pr_no, sha8):
        """Card@PR@Head —requestedToReview→ Agent(probe 마커 출처)."""
        return set(self._card_review_req.get(card_id, {}).get((pr_no, (sha8 or "").lower()), set()))

    def link_pr(self, card_id, repo, pr_no, sha=None, **prov):
        self.add_edge(card_id, "linkedTo", f"{repo}#{pr_no}",
                      repo=repo, pr=pr_no, sha=sha, **prov)


def build_graph(snap):
    """스냅샷 → Graph 투영. 결정적 — 동일 스냅샷은 동일 그래프."""
    g = Graph()
    for i in snap.get("issues") or []:
        comments = i.get("comments") or []
        g.set_comment_pr_repos(i["id"], parse_comment_pr_repos(comments))
        g.set_requested(i["id"], parse_requested(comments))
    return g
