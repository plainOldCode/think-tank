"""Validate completion reports, not the truth of externally executed tests."""
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _V2Stage(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class DesignStage(_V2Stage):
    criteria: str = Field(min_length=1)
    verification: str = Field(min_length=1)
    evidence: str = ""


class ImplementationStage(_V2Stage):
    summary: str = Field(min_length=1)
    commands: str = Field(min_length=1)


class EvidenceBlock(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    command: str = Field(min_length=1)
    exit_code: int
    output_snippet: str = ""
    note: str = ""


def _truncate_blocks(blocks):
    return [EvidenceBlock(command=b.command, exit_code=b.exit_code,
                          output_snippet=b.output_snippet[:2000], note=b.note) for b in blocks]


class VerificationStage(_V2Stage):
    commands: str = Field(min_length=1)
    evidence: str | list[dict] = Field(min_length=1)


class CompletionReport(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    contract_version: str = Field(min_length=1)
    attempt: int = Field(ge=1)
    method: Literal["tdd", "alternative", "planned"]
    red_command: str = ""
    red_evidence: str = ""
    reason: str = ""
    command: str = ""
    result: Literal["passed", "failed", "inconclusive"]
    evidence: str = ""
    limitations: str = ""
    design: DesignStage | None = None
    implementation: ImplementationStage | None = None
    verification: VerificationStage | None = None

    @model_validator(mode="after")
    def normalize_evidence(self):
        """계약 버전에 따라 verification.evidence를 정규화·검증.
        - v2.1+: 배열은 EvidenceBlock로 변환(command 필수, snippet 절단), 문자열은 유지.
        - v2 이하: 배열 거부(회귀 보호 — probe가 저장된 v2 보고를 verify하는 경로 보호).
        result=passed인데 exit_code!=0 블록이면 모순으로 거부."""
        stage = self.verification
        if stage is None or isinstance(stage.evidence, str):
            return self
        cv = self.contract_version
        if not cv.startswith("tt-tdd-v2.1"):
            raise ValueError("structured evidence requires tt-tdd-v2.1+")
        blocks = [EvidenceBlock(**b) for b in stage.evidence]
        if not blocks:
            raise ValueError("verification.evidence empty")
        if self.result == "passed" and any(b.exit_code != 0 for b in blocks):
            raise ValueError("result=passed contradicts a failed exit_code in evidence")
        stage.evidence = _truncate_blocks(blocks)
        return self

    @model_validator(mode="after")
    def method_evidence(self):
        if self.contract_version.startswith("tt-tdd-v2"):
            if self.command or self.evidence or self.red_command or self.red_evidence or self.reason:
                raise ValueError("v2 reports replace flat fields with the three stage blocks")
            if self.design is None or self.implementation is None or self.verification is None:
                raise ValueError("v2 report requires design, implementation and verification stages")
            if self.method == "alternative":
                raise ValueError("v2 uses planned instead of alternative")
            if self.method == "tdd" and not self.design.evidence:
                raise ValueError("v2 tdd requires design.evidence (pre-execution failure of the verification)")
            return self
        if self.design or self.implementation or self.verification:
            raise ValueError("stage blocks require a tt-tdd-v2 contract version")
        if not (self.command and self.evidence):
            raise ValueError("v1 report requires command and evidence")
        if self.method == "tdd" and not (self.red_command and self.red_evidence):
            raise ValueError("tdd requires red_command and red_evidence")
        if self.method == "alternative" and not self.reason:
            raise ValueError("alternative requires a reason for replacing TDD")
        return self


def legacy_result(body: str) -> str:
    """Conservative compatibility parser. Only structured reports bind a contract version."""
    text = re.sub(r"\b0 (?:failed|failures|errors)\b", "", body, flags=re.I)
    if re.search(r"\b(failed|failure|failures|errors?|inconclusive)\b|미실행|실행하지|통과하지|실패", text, re.I):
        return "failed"
    if re.search(r"\b[1-9]\d* passed\b|\btests? passed\b|\bRan [1-9]\d* tests?\b[\s\S]*\bOK\b", text, re.I):
        return "passed"
    if re.search(r"\bcommit\s+[0-9a-f]{7,40}\b", text, re.I) and "통과" in text:
        return "passed"
    return ""
