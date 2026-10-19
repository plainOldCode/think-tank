"""G5QS(M58SPW6V-G5QS): 반려 사유 → 타입 고정 트리플 enricher — general 버킷만 대상.

U5(M5A4HZAT-RNT4, 사용자 결정 A)로 판단기 어댑터는 퇴역 — 규칙 버킷 집계만
남는다. general 버킷 반려는 트리플 없이 counts["target"]만 늘린다.
"""
import sys

sys.path.insert(0, "server")
sys.path.insert(0, "scripts")

from enrich_rejections import enrich  # noqa: E402


def _rc_comment(cid, body, author="codex"):
    return {"id": cid, "author": author,
            "body": f"review: request-changes\nTT#M58SPW6V-G5QS@a1/{'a' * 64}\n{body}"}


def test_general_bucket_is_target_but_has_no_triples():
    """규칙 버킷이 general인 반려만 대상 — 어댑터 퇴역 후 트리플은 없다."""
    counts = {"target": 0, "accepted": 0, "rejected": 0}
    triples = enrich([_rc_comment(4, "구조가 애매하다")], "C", counts)
    assert triples == [] and counts == {"target": 1, "accepted": 0, "rejected": 0}


def test_decided_buckets_are_not_targeted():
    """규칙 버킷(test/migration/contract) 반려는 대상에서 제외된다."""
    counts = {"target": 0, "accepted": 0, "rejected": 0}
    triples = enrich([_rc_comment(1, "테스트가 실패한다"),           # test 버킷
                      _rc_comment(2, "마이그레이션 누락"),           # migration 버킷
                      _rc_comment(3, "계약 위반 — 스키마 변경")],    # contract 버킷
                     "C", counts)
    assert triples == [] and counts == {"target": 0, "accepted": 0, "rejected": 0}


def test_machine_authors_and_non_verdicts_skipped():
    counts = {"target": 0, "accepted": 0, "rejected": 0}
    triples = enrich([_rc_comment(1, "이유", author="probe"),
                      _rc_comment(2, "이유", author="tt-server"),
                      {"id": 3, "author": "codex", "body": "chat only"},
                      _rc_comment(4, "이유")], "C", counts)
    assert triples == [] and counts == {"target": 1, "accepted": 0, "rejected": 0}
