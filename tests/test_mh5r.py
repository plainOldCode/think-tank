"""MH5R(M58SPV08-MH5R) — 트리플 추출기 단위 테스트.

픽스처 코멘트 → 기대 트리플. 서버 코드 무변경 카드 — 이 테스트 파일만 tests/에 추가.
"""
import importlib.util
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "server"))  # verdicts·identity·ontology

_spec = importlib.util.spec_from_file_location(
    "extract_triples", os.path.join(_ROOT, "scripts", "extract_triples.py"))
et = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(et)


AGENTS = [{"name": "agy", "aliases": []}, {"name": "cursor-review", "aliases": []}]


@pytest.fixture(autouse=True)
def _reset_unknown():
    from ontology import catalog
    catalog.reset_unknown()
    yield
    catalog.reset_unknown()


def issue(iid="M4ABCDEF-GH12", attempt=1):
    return {"id": iid, "execution_attempt": attempt}


def c(cid, author, body):
    return {"id": cid, "author": author, "body": body}


SUBJ1 = "TT#M4ABCDEF-GH12@a1/" + "a" * 64
SUBJ2 = "TT#M4ABCDEF-GH12@a1/" + "b" * 64


def test_verdict_on_subject():
    comments = [c(1, "agy", f"review: approve\nTT#{'M4ABCDEF-GH12'}@a1/{'a'*64}\n근거")]
    triples = et.extract_issue(issue(), comments, AGENTS)
    assert {"s": SUBJ1, "p": "verdict", "o": "approve"} in triples
    assert {"s": SUBJ1, "p": "verdictBy", "o": "agy"} in triples


def test_machine_author_excluded():
    comments = [c(1, "probe", f"review: approve\nTT#M4ABCDEF-GH12@a1/{'a'*64}"),
                c(2, "runner@tp-13", f"review: approve\nTT#M4ABCDEF-GH12@a1/{'a'*64}")]
    assert not [t for t in et.extract_issue(issue(), comments, AGENTS)
                if t["p"] == "verdict"]


def test_unregistered_reviewer_not_promoted():
    comments = [c(1, "random-dude", f"review: approve\nTT#M4ABCDEF-GH12@a1/{'a'*64}")]
    triples = et.extract_issue(issue(), comments, AGENTS)
    assert not [t for t in triples if t["p"] == "verdictBy"]
    from ontology import catalog
    assert catalog.unknown_counts()["reviewer"] == 1  # id만 기록 — 본문 없음


def test_rejection_has_bucket_id_only():
    body = (f"review: request-changes\nTT#M4ABCDEF-GH12@a1/{'a'*64}\n"
            "테스트 회귀 — RED 없이 통과. 커버리지 확인 필요")
    triples = et.extract_issue(issue(), [c(7, "agy", body)], AGENTS)
    hb = [t for t in triples if t["p"] == "hasBucket"]
    assert hb and hb[0]["o"] == "test"
    assert hb[0]["s"] == "M4ABCDEF-GH12#c7"  # comment id만 — 본문 미저장
    assert hb[0]["subject"] == SUBJ1


def test_rework_at_attempt():
    triples = et.extract_issue(issue(attempt=3), [], AGENTS)
    assert {"s": "M4ABCDEF-GH12", "p": "reworkedAt", "o": "a2"} in triples
    assert {"s": "M4ABCDEF-GH12", "p": "reworkedAt", "o": "a3"} in triples
    assert not any(t["o"] == "a1" for t in triples if t["p"] == "reworkedAt")


def test_supersedes_within_same_attempt():
    comments = [c(1, "tt-server", f"판정 대상 {SUBJ1}"),
                c(2, "agy", f"review: request-changes\n{SUBJ1}\n수정 필요"),
                c(3, "tt-server", f"판정 대상 {SUBJ2}"),  # 재제출 — 새 subject
                c(4, "agy", f"review: approve\n{SUBJ2}")]
    triples = et.extract_issue(issue(), comments, AGENTS)
    assert {"s": SUBJ2, "p": "supersedes", "o": SUBJ1} in triples


def test_no_body_storage_in_output():
    comments = [c(1, "agy", f"review: request-changes\n{SUBJ1}\n시크릿 같은 본문 내용 password=xyz")]
    triples = et.extract_issue(issue(), comments, AGENTS)
    blob = repr(triples)
    assert "password" not in blob and "본문 내용" not in blob


def test_query_rejects_counts():
    triples = [
        {"s": "C1#c1", "p": "hasBucket", "o": "test", "subject": "s1"},
        {"s": "C1#c2", "p": "hasBucket", "o": "test", "subject": "s1"},
        {"s": "C2#c3", "p": "hasBucket", "o": "contract", "subject": "s2"},
    ]
    q = et.query_rejects(triples)
    assert q["cards_ge2_rejections"] == 1 and q["repeat_cards"] == ["C1"]
    assert {"bucket": "test", "cards": 1} in q["buckets"]


def test_query_family_skew():
    triples = [
        {"s": "s1", "p": "verdictBy", "o": "agy"},
        {"s": "s1", "p": "verdictBy", "o": "cursor-review"},
        {"s": "s2", "p": "verdictBy", "o": "agy"},
    ]
    q = et.query_family_skew(triples)
    # agy=계열 미지, cursor-review=cursor — s1은 2계열
    assert q["subjects_reviewed"] == 2
    assert q["subjects_multi_family"] == 1 and "s1" in q["detail"]
