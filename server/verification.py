"""Validate completion reports, not the truth of externally executed tests."""
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CompletionReport(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    contract_version: str = Field(min_length=1)
    attempt: int = Field(ge=1)
    method: Literal["tdd", "alternative"]
    red_command: str = ""
    red_evidence: str = ""
    reason: str = ""
    command: str = Field(min_length=1)
    result: Literal["passed", "failed", "inconclusive"]
    evidence: str = Field(min_length=1)
    limitations: str = ""

    @model_validator(mode="after")
    def method_evidence(self):
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
