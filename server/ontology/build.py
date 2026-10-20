"""probe 관계 그래프 투영 (NB5T, RD §4.1·§4.4) — read-only, DB 쓰기 없음.

스냅샷의 코멘트·PR을 한 번 투영해 decide의 문자열 재파싱을 질의로 바꾼다.
노드는 카탈로그 통과분만(unknown은 기록만), edge는 provenance(comment_id·
author·attempt·repo·pr·sha)를 갖는다. 동일 스냅샷 → 동일 그래프(결정적).
이 모듈은 core를 임포트하지 않는다(단방향 — core가 이 모듈을 소비).
"""
import re

PR_URL = re.compile(r"https://github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)")


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



class Graph:
    """카드 중심 관계 질의 — decide의 재파싱 지점을 대체한다(NB5T 단계 치환).

    P5(M5BH91TA-S28F) — 레거시 [review-req] 질의(parse_requested·requested_reviewers)와
    edge 저장소(add_edge·has_edge·edges·link_pr)는 prod 리더가 없어 제거했다. 유일
    질의는 comment_pr_repos(core.py decide ③). edge provenance는 문서 RD §4.1 역사.
    """

    def __init__(self):
        self._card_pr_urls = {}     # card_id → [(repo, pr_no)]

    def set_comment_pr_repos(self, card_id, pairs):
        self._card_pr_urls[card_id] = list(pairs)

    def comment_pr_repos(self, card_id):
        """Card —commentUrl→ PR — 코멘트 등장 순 중복 제거."""
        return list(self._card_pr_urls.get(card_id) or [])


def build_graph(snap):
    """스냅샷 → Graph 투영. 결정적 — 동일 스냅샷은 동일 그래프."""
    g = Graph()
    for i in snap.get("issues") or []:
        g.set_comment_pr_repos(i["id"], parse_comment_pr_repos(i.get("comments") or []))
    return g
