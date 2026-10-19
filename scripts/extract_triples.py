#!/usr/bin/env python3
"""MH5R(M58SPV08-MH5R) — 오프라인 트리플 추출기(읽기 전용) + count 질의.

NVP 단계 0~1: 서버 무변경. §4-2 관계 규칙 4종을 JSONL 트리플로 투영한다 —
id만 저장하고 원문 본문은 어디에도 남기지 않는다(TV Q4). 기계 author는
verdicts.is_machine_author로, 카탈로그 미등록 엔티티는 ontology.catalog로 걸러낸다
(XKRY). 정확률은 이 단계에서 측정하지 않는다(카드 명시).

규칙 4종:
- Verdict on Subject: 리뷰어 판정 코멘트 → (subject, "verdict", type),
  (subject, "verdictBy", reviewer). 판정은 review_subject 토큰과 본문 첫 줄
  판정어(verdicts.first_line_verdict)가 모두 있을 때만.
- Subject supersedes: 같은 (카드, 회차)에서 코멘트에 등장한 서로 다른 subject
  토큰의 첫 언급 순서 — 연속한 쌍을 (신규, "supersedes", 구)로 기록(재제출).
- Card reworkedAt Attempt: execution_attempt n>=2마다 (카드, "reworkedAt", f"a{n}").
- Rejection hasBucket: request-changes 판정 코멘트 → (comment_id, "hasBucket", 버킷).
  버킷 분류는 verdicts.classify_rejection(판정 헤더 제외 본문).

질의(count 전용):
  --query rejects       버킷별 반려 카드 수 + 반려 2회 이상 카드 수(369Y 재현)
  --query family-skew   같은 subject를 리뷰한 리뷰어 계열 편중(subject별 계열 집합)

사용:
  TT_URL=http://<host>:7800 python3 scripts/extract_triples.py [--query ...] [--out f.jsonl]
"""
import argparse
import json
import os
import re
import sys
import urllib.request

# server/ 모듈(verdicts·identity·ontology.catalog) — 레포 트리에서 평면 import
for _rp in (os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            os.path.expanduser("~/think-tank")):
    if os.path.isdir(os.path.join(_rp, "server")):
        sys.path.insert(0, os.path.join(_rp, "server"))
        break

from verdicts import first_line_verdict, is_machine_author, classify_rejection  # 9WWK 단일 소스
from identity import agent_family
from ontology import catalog  # XKRY: 미등록 엔티티 게이트(id만 기록, 본문 금지)

# review_subject 토큰 — TT#<카드id>@a<n>/<sha256 64hex>
SUBJECT_RE = re.compile(r"TT#[A-Z0-9]{6,12}-[A-Z0-9]{3,6}@a\d+/[0-9a-f]{64}")


def api(url, path):
    req = urllib.request.Request(url.rstrip("/") + path,
                                 headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def fetch_all(url):
    """읽기 전용 수집 — /issues(전체)·/agents·리뷰 이력 카드 상세(comments)."""
    issues, offset = [], 0
    while True:
        page = api(url, f"/issues?limit=1000&offset={offset}")
        if not page:
            break
        issues.extend(page)
        offset += len(page)
    agents = api(url, "/agents")
    detail = {}
    for i in issues:
        d = api(url, f"/issues/{i['id']}")
        if isinstance(d.get("comments"), list):
            detail[i["id"]] = d
    return issues, agents, detail


def _resolve_reviewer(author, agents):
    """리뷰어 정본(정확명·별칭만). 미등록은 카탈로그에 기록되고 None — 제외."""
    return catalog.resolve("reviewer", author, agents=agents)


def extract_issue(issue, comments, agents):
    """카드 1장 → 트리플 목록 (id만 — 본문 저장 금지). 순서 결정적."""
    triples = []
    card = catalog.resolve("card", issue["id"])
    if not card:
        return triples  # 형식 불일치 카드 — 노드 승격 없음(unknown만 기록됨)
    attempt = int(issue.get("execution_attempt") or 0)
    for n in range(2, attempt + 1):  # reworkedAt: 재작업 회차마다
        triples.append({"s": card, "p": "reworkedAt", "o": f"a{n}"})

    # 코멘트에서 subject 토큰·판정 수집 — 기계 author 제외, 본문은 저장하지 않는다
    seen_subjects = []  # (첫 언급 순) (card, attempt) 내 distinct 토큰
    for c in comments or []:
        author = (c.get("author") or "").strip()
        if is_machine_author(author):
            continue
        body = c.get("body") or ""
        tokens = SUBJECT_RE.findall(body)
        verdict = first_line_verdict(body)
        if not tokens:
            continue
        tok = tokens[0]
        if tok not in [t for t, _ in seen_subjects]:
            seen_subjects.append((tok, c.get("id")))
        if not verdict:
            continue
        reviewer = _resolve_reviewer(author, agents)
        if not reviewer:
            continue  # 카탈로그 미등록 — 노드 승격 없음(unknown 기록만)
        triples.append({"s": tok, "p": "verdict", "o": verdict})
        triples.append({"s": tok, "p": "verdictBy", "o": reviewer})
        if verdict == "request-changes":
            lines = body.strip().splitlines()
            bucket = classify_rejection("\n".join(lines[1:]) if len(lines) > 1 else body)
            triples.append({"s": f"{card}#c{c.get('id')}", "p": "hasBucket", "o": bucket,
                            "subject": tok})

    # supersedes: 동일 (카드, 회차) 내 distinct 토큰의 첫 언급 순서 → 연속 쌍
    cur_attempt = f"a{attempt}"
    chain = [t for t, _cid in seen_subjects if f"@{cur_attempt}/" in t]
    for old, new in zip(chain, chain[1:]):
        triples.append({"s": new, "p": "supersedes", "o": old})
    return triples


def query_rejects(triples):
    """반려×버킷 count — 버킷별 카드 수 + 반려 2회 이상 카드(369Y '반려 2회 이상 22장' 재현)."""
    by_bucket, per_card = {}, {}
    for t in triples:
        if t["p"] != "hasBucket":
            continue
        by_bucket.setdefault(t["o"], set()).add(t["s"].split("#c")[0])
        per_card[t["s"].split("#c")[0]] = per_card.get(t["s"].split("#c")[0], 0) + 1
    buckets = sorted(({"bucket": b, "cards": len(cs)} for b, cs in by_bucket.items()),
                     key=lambda x: -x["cards"])
    repeat = sorted(k for k, v in per_card.items() if v >= 2)
    return {"buckets": buckets, "cards_ge2_rejections": len(repeat), "repeat_cards": repeat}


def query_family_skew(triples):
    """같은 subject 리뷰어 계열 편중 — subject별 리뷰어 계열 집합(정확률 측정 없음)."""
    fams = {}
    for t in triples:
        if t["p"] != "verdictBy":
            continue
        fams.setdefault(t["s"], set()).add(agent_family(t["o"]) or "unknown")
    skew = {s: sorted(f) for s, f in fams.items() if len(f) > 1}
    return {"subjects_reviewed": len(fams),
            "subjects_multi_family": len(skew), "detail": skew}


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--query", choices=["rejects", "family-skew"], default=None,
                    help="count 전용 질의(트리플 생성 대신 요약 출력)")
    ap.add_argument("--out", default=None, help="JSONL 저장 경로(기본 stdout)")
    args = ap.parse_args()
    url = os.environ.get("TT_URL")
    if not url:
        sys.exit("TT_URL 환경변수 필요")
    issues, agents, detail = fetch_all(url)
    triples = []
    for i in issues:
        d = detail.get(i["id"], {})
        triples.extend(extract_issue(dict(i), d.get("comments") or [], agents))
    if args.query:
        q = query_rejects(triples) if args.query == "rejects" else query_family_skew(triples)
        json.dump(q, sys.stdout, ensure_ascii=False, indent=1)
        print()
        return
    out = open(args.out, "w") if args.out else sys.stdout
    with out:
        for t in triples:
            out.write(json.dumps(t, ensure_ascii=False) + "\n")
    if args.out:
        print(f"{len(triples)} triples → {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
