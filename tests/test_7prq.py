"""7PRQ(M58SPWKK-7PRQ): 사람 go/stop 결정 마커 — 형식 결정(사용자 2026-10-19),
형식 위반 거부, author 자기신고 불인정, 동작 불변(기록·집계만)."""
import ontology
from ontology import parse_decision

SUBJECT = "TT#M58SPWKK-7PRQ@a1/" + "b" * 64
BODY = f"decision: go\n{SUBJECT}\nno_pr"


def test_valid_marker_parsed():
    d = parse_decision(BODY, human=True, actor="skshim", comment_id=42)
    assert d == {"decision": "go", "subject": SUBJECT, "reason_code": "no_pr",
                 "actor": "skshim", "comment_id": 42}
    assert parse_decision(f"decision: stop\n{SUBJECT}\nbudget_exhausted",
                          human=True)["decision"] == "stop"


def test_format_violations_rejected():
    """수용기준 1 — 형식 위반은 거부한다."""
    assert parse_decision("decision: maybe\n" + SUBJECT + "\nno_pr", human=True) is None
    assert parse_decision("decision: go", human=True) is None  # 3행 미만
    assert parse_decision(f"decision: go\n\nno_pr", human=True) is None  # 빈 subject
    assert parse_decision(f"decision: go\n{SUBJECT}\nBad Code", human=True) is None
    assert parse_decision(f"decision: go\n{SUBJECT}\nno-pr", human=True) is None


def test_author_self_report_is_not_a_decision():
    """수용기준 2 — author만으로는 사람 결정으로 인정하지 않는다."""
    # 별도 인정 경로(human=True) 없이는 아무 author의 마커도 기록이 아니다.
    assert parse_decision(BODY, human=False, actor="skshim@tp-13") is None


def test_exported_from_package():
    assert ontology.parse_decision is parse_decision
