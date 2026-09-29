import pytest
from pydantic import ValidationError

from verification import CompletionReport

V2 = "tt-tdd-v2:deadbeef"
V1 = "tt-tdd-v1:deadbeef"


def v2_body(**over):
    body = {
        "contract_version": V2,
        "attempt": 1,
        "method": "tdd",
        "design": {
            "criteria": "v2 보고서는 3단계 필수",
            "verification": "pytest tests/test_report_v2.py — 수정 전 이 파일이 실패",
            "evidence": "수정 전 실행: 6 failed",
        },
        "implementation": {"summary": "v2 스키마 추가", "commands": "edit server/verification.py"},
        "verification": {"commands": "pytest tests/ -q", "evidence": "전체 통과"},
        "result": "passed",
        "limitations": "",
    }
    body.update(over)
    return body


def test_v2_requires_all_three_stages():
    for stage in ("design", "implementation", "verification"):
        body = v2_body()
        body.pop(stage)
        with pytest.raises(ValidationError):
            CompletionReport(**body)


def test_v2_design_needs_criteria_and_verification():
    for key in ("criteria", "verification"):
        stage = dict(v2_body()["design"])
        stage[key] = ""
        with pytest.raises(ValidationError):
            CompletionReport(**v2_body(design=stage))


def test_v2_tdd_needs_pre_execution_failure_evidence():
    stage = dict(v2_body()["design"])
    stage["evidence"] = ""
    with pytest.raises(ValidationError):
        CompletionReport(**v2_body(design=stage))


def test_v2_planned_needs_no_red_evidence():
    assert CompletionReport(**v2_body(method="planned", design={
        "criteria": "분석 결론이 원문 위치와 대조 가능",
        "verification": "발췌 재추적 체크리스트(오류 시 실패)",
    })).method == "planned"


def test_v2_rejects_legacy_alternative_and_old_flat_fields():
    with pytest.raises(ValidationError):
        CompletionReport(**v2_body(method="alternative"))
    with pytest.raises(ValidationError):
        CompletionReport(**v2_body(command="pytest", evidence="통과"))


def test_v2_verification_needs_commands_and_evidence():
    for key in ("commands", "evidence"):
        stage = dict(v2_body()["verification"])
        stage[key] = ""
        with pytest.raises(ValidationError):
            CompletionReport(**v2_body(verification=stage))


def test_v1_report_shape_unchanged():
    CompletionReport(contract_version=V1, attempt=1, method="tdd",
                     red_command="pytest", red_evidence="1 failed",
                     command="pytest", result="passed", evidence="1 passed")
    CompletionReport(contract_version=V1, attempt=1, method="alternative",
                     reason="문서 작업", command="grep", result="passed", evidence="통과")
    with pytest.raises(ValidationError):
        CompletionReport(**v2_body(contract_version=V1))
