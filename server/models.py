"""API 스키마 — pydantic 모델 (라우팅/로직과 분리)."""
from pydantic import BaseModel

from verification import CompletionReport


class IssueCreate(BaseModel):
    title: str
    body: str = ""
    parent_id: str | None = None
    priority: int | None = None
    labels: list[str] = []
    state: str = "todo"


class ClaimIn(BaseModel):
    agent: str
    require_label: str | None = None
    hours: int = 6

    def safe_hours(self):
        return max(1, min(6, self.hours))


class ReviewClaimIn(BaseModel):
    """리뷰어 claim — review 상태 카드에 판정자 점유 표기. 상태·계약·attempt 불변."""

    agent: str
    hours: int = 2

    def safe_hours(self):
        return max(1, min(6, self.hours))


class LeaseIn(BaseModel):
    agent: str
    hours: int = 6

    def safe_hours(self):
        return max(1, min(6, self.hours))


class IssuePatch(BaseModel):
    title: str | None = None
    body: str | None = None
    state: str | None = None
    parent_id: str | None = None
    clear_parent: bool = False
    priority: int | None = None
    clear_priority: bool = False
    labels: list[str] | None = None
    assignee: str | None = None
    # M42KC1XR-2DD1: 리뷰어 점유 해제 — probe가 판정 기록 후 반납
    reviewer: str | None = None
    expected_version: int | None = None
    archived: bool | None = None
    # blocked 사족 (M3BZS1FS-5722 ①): 미입력 시 기존 동작 유지
    waiting_for: str | None = None
    waiting_actor: str | None = None
    blocked_detail: str | None = None
    # 완료 보고 게이트: 성공 결과 없으면 review. force_done/close는 명시적 승인 예외.
    force_done: bool = False
    completion_report: CompletionReport | None = None


class VerifyIn(BaseModel):
    verifier: str
    evidence: str = ""
    completion_report: CompletionReport | None = None
    expected_version: int | None = None
    human: bool = False  # THNJ 약한 합의: 사람 자기선언 — 감사 표시 전용, 인증 아님


class CommentIn(BaseModel):
    author: str
    body: str


class AgentIn(BaseModel):
    name: str
    base_url: str
    secret: str = ""
    enabled: bool = True
    release_hook: bool = False  # reconcile release 명령 수신 능력 (M3BZS1FS-5722 ③)
    notify_hook: bool = False  # blocked(human) Level4 알림 수신 능력 (M3BZV172-9F0S B)


class AgentPatch(BaseModel):
    base_url: str | None = None
    secret: str | None = None
    enabled: bool | None = None
    release_hook: bool | None = None
    notify_hook: bool | None = None


class DispatchIn(BaseModel):
    agent: str
    message: str
    author: str = "board"


class DispatchProgress(BaseModel):
    """러너→서버 진행 투영 (TT M3EREF97-FXWQ). 전부 선택, unknown 필드 무시."""
    state: str | None = None
    tail: str | None = None
    ts: str | None = None
    machine: str | None = None
    session: str | None = None
