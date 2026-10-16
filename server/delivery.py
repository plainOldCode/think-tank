"""Selected delivery is part of the reviewed report; PRs remain optional."""
import json
import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


REPORT_MODES = frozenset({"report", "local"})


class Delivery(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    mode: Literal["report", "local", "pr"]
    provider: Literal["github", "gitea"] | None = None
    repo: str | None = Field(default=None, pattern=r"^[\w.-]+/[\w.-]+$")
    number: int | None = Field(default=None, ge=1)
    head_sha: str | None = Field(default=None, pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    base: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def selected_target(self):
        fields = (self.provider, self.repo, self.number, self.head_sha, self.base)
        if self.mode == "pr" and not all(fields):
            raise ValueError("PR delivery requires provider, repo, number, full head_sha and base")
        if self.mode in REPORT_MODES and any(v is not None for v in fields):
            raise ValueError("report delivery must not include a PR target")
        return self


class MergeReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: Literal["github", "gitea"]
    repo: str
    number: int = Field(ge=1)
    head_sha: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    base: str
    merge_commit: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    merged_at: datetime


TARGET_KEYS = ("provider", "repo", "number", "head_sha", "base")


def requirement(issue, linked=False):
    report = issue.get("completion_report") or {}
    spec = report.get("delivery")
    if spec:
        return spec
    # A legacy PR reference is not evidence of local-only intent. Require an
    # explicit selection rather than guessing a head from prose/old comments.
    text = json.dumps(report, ensure_ascii=False) + "\n" + (issue.get("body") or "")
    text += "\n" + "\n".join(c.get("body") or "" for c in issue.get("comments") or [])
    if linked or re.search(r"https?://[^\s\"<>]+/(?:pull|pulls)/\d+|\bPR\s*#\d+", text):
        return {"mode": "unbound-pr"}
    return {"mode": "local"}


def readiness(spec, observed, patch_ids=None):
    """None = a matching open/merged target is ready for review/delivery.

    patch_ids(SRJY): {(repo, sha): git patch-id} — rebase로 head가 바뀌어도
    patch-id가 같으면 같은 산출물이다(base 동일 전제). head만 재검사를 건너뛴다.
    """
    if spec.get("mode") == "unbound-pr":
        return "PR 연결 보고에 delivery(provider/repo/number/head_sha/base)를 명시해 재제출 필요"
    if spec.get("mode") != "pr":
        return None
    if not observed or observed.get("state") == "UNKNOWN":
        return "PR 관측 불가 — 조회 실패/미지원 provider를 report 완료로 대체하지 않음"
    if any(spec.get(k) != observed.get(k) for k in TARGET_KEYS):
        if spec.get("base") == observed.get("base") and patch_ids and \
                spec.get("head_sha") and observed.get("head_sha") and \
                patch_ids.get((spec.get("repo"), spec.get("head_sha"))) and \
                patch_ids.get((spec.get("repo"), spec.get("head_sha"))) == \
                patch_ids.get((observed.get("repo"), observed.get("head_sha"))):
            pass  # SRJY: rebase — patch-id 동일, 같은 변경이므로 재제출 불필요
        else:
            return "PR 대상/head/base 변경 — 현재 산출물로 보고 재제출·재리뷰 필요"
    if observed.get("state") not in ("OPEN", "MERGED"):
        return "PR 미병합 폐기 — 반영 경로 확인 필요"
    if observed.get("isDraft"):
        return "draft PR — 반영 준비 대기"
    checks = observed.get("checks")
    if checks is None or any(c.get("state") != "SUCCESS" for c in checks):
        return "CI 미통과/관측 불가 — 완료 보류"
    if observed["state"] == "MERGED":
        try:
            MergeReceipt.model_validate({k: observed.get(k) for k in
                                         (*TARGET_KEYS, "merge_commit", "merged_at")})
        except ValueError:
            return "병합 receipt 미확인 — 완료 보류"
    return None
