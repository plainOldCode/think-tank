"""84R6(M5283K1Q-84R6) — 판정 단계 enum 도입과 보고 스키마 단일화.

verification.verification_level: live-verified | unit-test-verified |
type-check-only | planned-only | blocked. result=passed는 행동 검증 단계
(live/unit-test)에서만 허용 — "CI green은 판정이 아니다". v2.2 계약 보고는 필수,
v2.1은 선택(호환). 상위 flat 필드(red_*·command·evidence)는 v2 계약 쓰기에서 이미
금지 — 읽기(v1)만 유지.
"""
import pytest
from pydantic import ValidationError

from verification import CompletionReport


def _v21(**over):
    body = {
        "contract_version": "tt-tdd-v2.1:abc",
        "attempt": 1,
        "method": "tdd",
        "design": {"criteria": "c", "verification": "v", "evidence": "red: 1 failed"},
        "implementation": {"summary": "s", "commands": "cmd"},
        "verification": {"commands": "pytest -q",
                         "evidence": [{"command": "pytest -q", "exit_code": 0,
                                       "output_snippet": "5 passed"}]},
        "result": "passed",
        "limitations": "",
    }
    body.update(over)
    return body


def test_v21_보고에_판정_단계를_붙일_수_있다():
    rep = CompletionReport(**_v21(verification={"commands": "pytest -q",
                                                "evidence": [{"command": "pytest -q", "exit_code": 0}],
                                                "verification_level": "unit-test-verified"}))
    assert rep.verification.verification_level == "unit-test-verified"


def test_passed는_행동_검증_단계에서만_허용된다():
    for level in ("type-check-only", "planned-only", "blocked"):
        with pytest.raises(ValidationError):
            CompletionReport(**_v21(verification={"commands": "x", "evidence": [
                {"command": "x", "exit_code": 0}], "verification_level": level}))


def test_실패_무판정_보고는_모든_단계를_허용한다():
    for result in ("failed", "inconclusive"):
        for level in ("type-check-only", "planned-only", "blocked",
                      "unit-test-verified", "live-verified"):
            rep = CompletionReport(**_v21(verification={"commands": "x", "evidence": [
                {"command": "x", "exit_code": 1}], "verification_level": level},
                result=result))
            assert rep.verification.verification_level == level


def test_알_수_없는_단계는_거부된다():
    with pytest.raises(ValidationError):
        CompletionReport(**_v21(verification={"commands": "x", "evidence": [
            {"command": "x", "exit_code": 0}], "verification_level": "vibes-verified"}))


def test_v21_단계_생략은_하위_호환으로_수용된다():
    rep = CompletionReport(**_v21())
    assert rep.verification.verification_level is None


def test_v22_보고는_판정_단계가_필수다():
    with pytest.raises(ValidationError):
        CompletionReport(**_v21(contract_version="tt-tdd-v2.2:abc"))
    rep = CompletionReport(**_v21(contract_version="tt-tdd-v2.2:abc",
                                  verification={"commands": "pytest -q",
                                                "evidence": [{"command": "pytest -q", "exit_code": 0}],
                                                "verification_level": "unit-test-verified"}))
    assert rep.verification.verification_level == "unit-test-verified"


def test_v22에서도_구조화_evidence와_flat_금지가_유지된다():
    with pytest.raises(ValidationError):
        CompletionReport(**_v21(contract_version="tt-tdd-v2.2:abc",
                                verification={"commands": "pytest -q",
                                              "evidence": [{"command": "pytest -q", "exit_code": 0}],
                                              "verification_level": "unit-test-verified"},
                                command="pytest", evidence="통과"))


def test_계약_v22가_env로_선택된다(monkeypatch):
    import work_contract
    monkeypatch.setenv("TT_CONTRACT_VERSION", "2.2")
    c = work_contract.current_contract()
    assert c["version"].startswith("tt-tdd-v2.2:")
    assert "verification_level" in c["instructions"]
    # 기본값은 여전히 v2.1 — 배포 후 명시 전환
    monkeypatch.delenv("TT_CONTRACT_VERSION")
    assert work_contract.current_contract()["version"].startswith("tt-tdd-v2.1:")
