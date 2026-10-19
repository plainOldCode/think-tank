"""반려 사유 → 타입 고정 트리플 LLM enricher (G5QS, NVP §4-2).

MH5R extract_triples.py의 hasBucket 위에, 규칙 버킷이 'general'인 반려만 대상으로
`Rejection cites {File|Contract|Test}` 트리플을 추가한다. 타입 고정 enum —
enum 밖 모델 출력은 버림+count. 모델 미구성·오류 시 규칙 버킷만 남는다
(fail-to-rules). 서버 코드 무변경, probe 판정에 연결하지 않는다(advisory).
트리플은 id만 저장하고 본문은 저장하지 않는다(TV Q4) — 본문은 모델 입력으로만 쓴다.

  TT_URL=http://<host>:7800 python3 scripts/enrich_rejections.py [--model judge|none] [--out f.jsonl]
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "server"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from extract_triples import SUBJECT_RE, api, fetch_all  # noqa: E402
from ontology import catalog  # noqa: E402
from verdicts import classify_rejection, first_line_verdict, is_machine_author  # noqa: E402

CITES_ENUM = ("File", "Contract", "Test")


def enrich(comments, card, counts=None):
    """카드 1장의 코멘트 → cites 트리플(id만) + counts 누적.

    대상: request-changes 판정 코멘트 중 규칙 버킷이 general인 것.
    U5(M5A4HZAT-RNT4) — 판단기 어댑터는 퇴역(골격 미구성). 규칙 버킷 집계만
    남긴다: general 버킷은 트리플 없이 counts["target"]만 늘린다."""
    counts = counts if counts is not None else {"target": 0, "accepted": 0, "rejected": 0}
    triples = []
    for c in comments or []:
        author = (c.get("author") or "").strip()
        if is_machine_author(author):
            continue
        body = c.get("body") or ""
        if first_line_verdict(body) != "request-changes":
            continue
        tokens = SUBJECT_RE.findall(body)
        if not tokens:
            continue
        lines = body.strip().splitlines()
        if classify_rejection("\n".join(lines[1:]) if len(lines) > 1 else body) != "general":
            continue  # 규칙 버킷이 결정된 반려 — enrich 대상 아님
        counts["target"] += 1
    return triples


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default="rejection-cites.jsonl")
    args = ap.parse_args()
    url = os.environ.get("TT_URL", "http://127.0.0.1:7800")
    issues, _agents, detail = fetch_all(url)
    counts = {"target": 0, "accepted": 0, "rejected": 0}
    triples = []
    for i in issues:
        d = detail.get(i["id"]) or {}
        card = catalog.resolve("card", i["id"])
        if not card:
            continue  # 형식 불일치 카드 — 투영 없음(unknown만 기록됨)
        triples.extend(enrich(d.get("comments"), card, counts))
    with open(args.out, "w", encoding="utf-8") as f:
        for t in triples:
            f.write(json.dumps(t, ensure_ascii=False) + "\n")
    print(json.dumps({"file": args.out, **counts}, ensure_ascii=False))


if __name__ == "__main__":
    main()
