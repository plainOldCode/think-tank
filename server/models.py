"""API 스키마 — pydantic 모델 (라우팅/로직과 분리)."""
from pydantic import BaseModel, model_validator

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
    # M3ER6G3S-RZ20: 실행 모델 메타데이터 — 선언(declaration)일 뿐 실행 보장이 아니다.
    # model/reasoning은 자유 문자열(검증 없음), tier는 정규화된 계층 enum.
    model: str | None = None
    reasoning: str | None = None
    tier: str | None = None

    @model_validator(mode="after")
    def normalize_tier(self):
        self.tier = _normalize_tier(self.tier)
        return self


TIER_ALIASES = {"판정": "sota", "실행": "exec", "구형": "impl"}
TIERS = {"sota", "exec", "impl", "human"}


def _normalize_tier(tier):
    """tier 정규화 — 한글 별칭(판정/실행/구형)→sota/exec/impl, 대소문자 무시. None 통과."""
    if tier is None:
        return None
    t = tier.strip().lower()
    if not t:
        return None
    t = TIER_ALIASES.get(t, t)
    if t not in TIERS:
        raise ValueError(f"tier는 {sorted(TIERS)} (판정|실행|구형 별칭 허용)")
    return t


class AgentPatch(BaseModel):
    base_url: str | None = None
    secret: str | None = None
    enabled: bool | None = None
    release_hook: bool | None = None
    notify_hook: bool | None = None
    model: str | None = None
    reasoning: str | None = None
    tier: str | None = None

    @model_validator(mode="after")
    def normalize_tier(self):
        self.tier = _normalize_tier(self.tier)
        return self


class DispatchIn(BaseModel):
    agent: str
    message: str
    author: str = "board"
    # M42KC1XR-2DD1 R2: 리뷰 dispatch는 구현 계약 대신 리뷰 계약을 전달
    work_contract: dict | None = None


class DispatchProgress(BaseModel):
    """러너→서버 진행 투영 (TT M3EREF97-FXWQ). 전부 선택, unknown 필드 무시."""
    state: str | None = None
    tail: str | None = None
    ts: str | None = None
    machine: str | None = None
    session: str | None = None
