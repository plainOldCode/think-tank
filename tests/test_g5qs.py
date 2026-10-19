"""G5QS(M58SPW6V-G5QS): 반려 사유 → 타입 고정 트리플 enricher — enum 거부,
fail-to-rules, general 버킷만 대상, 서버 코드 무변경."""
import sys

sys.path.insert(0, "server")
sys.path.insert(0, "scripts")

from enrich_rejections import CITES_ENUM, enrich  # noqa: E402


def _rc_comment(cid, body, author="codex"):
    return {"id": cid, "author": author,
            "body": f"review: request-changes\nTT#M58SPW6V-G5QS@a1/{'a' * 64}\n{body}"}


def test_enum_outside_output_rejected():
    """수용기준 1 — enum 밖 출력은 버림+count, 트리플 없음."""
    counts = {"target": 0, "accepted": 0, "rejected": 0}
    triples = enrich([_rc_comment(1, "설계 방향이 애매하다 — 어느 쪽으로 정리할지")],
                     "M58SPW6V-G5QS", propose=lambda body: "tests/test_x.py", counts=counts)
    assert triples == [] and counts["rejected"] == 1 and counts["accepted"] == 0
    for out in CITES_ENUM:  # enum 안은 채택
        assert enrich([_rc_comment(2, "문제")], "C", propose=lambda b, o=out: o)[0]["o"] == out


def test_no_model_is_fail_to_rules():
    """수용기준 2 — 모델 미구성이면 규칙 버킷만 남고 cites는 없다(예외 없음)."""
    counts = {"target": 0, "accepted": 0, "rejected": 0}
    triples = enrich([_rc_comment(1, "먼가 general 한 이유")], "C", propose=None,
                     counts=counts)
    assert triples == [] and counts["target"] == 1 and counts["accepted"] == 0


def test_model_error_is_rejected_not_raised():
    def boom(body):
        raise RuntimeError("llm down")
    counts = {"target": 0, "accepted": 0, "rejected": 0}
    triples = enrich([_rc_comment(1, "이유")], "C", propose=boom, counts=counts)
    assert triples == [] and counts["rejected"] == 1


def test_only_general_bucket_targeted():
    """규칙 버킷(test/migration/contract) 반려는 대상에서 제외된다."""
    counts = {"target": 0, "accepted": 0, "rejected": 0}
    triples = enrich([_rc_comment(1, "테스트가 실패한다"),           # test 버킷
                      _rc_comment(2, "마이그레이션 누락"),           # migration 버킷
                      _rc_comment(3, "계약 위반 — 스키마 변경"),     # contract 버킷
                      _rc_comment(4, "구조가 애매하다")],            # general
                     "C", propose=lambda b: "File", counts=counts)
    assert counts["target"] == 1
    assert len(triples) == 1 and triples[0]["o"] == "File"
    assert triples[0]["s"] == "C#c4" and triples[0]["p"] == "cites"


def test_machine_authors_and_non_verdicts_skipped():
    counts = {"target": 0, "accepted": 0, "rejected": 0}
    triples = enrich([_rc_comment(1, "이유", author="probe"),
                      _rc_comment(2, "이유", author="tt-server"),
                      {"id": 3, "author": "codex", "body": "chat only"},
                      _rc_comment(4, "이유")], "C", propose=lambda b: "Test",
                     counts=counts)
    assert counts["target"] == 1 and len(triples) == 1
    assert triples[0]["o"] == "Test" and triples[0]["subject"].startswith("TT#")
