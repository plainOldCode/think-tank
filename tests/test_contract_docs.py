"""PR#104 리뷰 P2-2 회귀 — 계약 블록은 server/static/api.md와 skills/tt/references/api.md가
항상 일치해야 한다. 스킬 사본은 번들 스냅숏이라 어긋나면 에이전트가 구 계약을 읽는다."""
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
MARKERS = ("tt-work-contract", "tt-work-contract-v2", "tt-work-contract-v2.1", "tt-work-contract-v2.2")


def _blocks(path):
    s = (ROOT / path).read_text()
    return {m: s.split(f"<!-- {m}:start -->", 1)[1].split(f"<!-- {m}:end -->", 1)[0].strip()
            for m in MARKERS}


def test_계약_블록은_서버와_스킬_사본이_일치한다():
    src, ref = _blocks("server/static/api.md"), _blocks("skills/tt/references/api.md")
    for m in MARKERS:
        assert src[m] == ref[m], f"{m} 블록 불일치 — skills/tt/references/api.md를 재동기화할 것"


def test_제출_동사는_submit이_정본이다():
    """done --report 안내 잔재 회귀 — live 문서에는 tt submit이 정본으로 있어야 한다."""
    for f in ("server/static/api.md", "skills/tt/SKILL.md"):
        s = (ROOT / f).read_text()
        assert "tt submit" in s, f"{f}: tt submit 문서화 없음"


def test_tt_submission_identity_is_delivered_in_every_contract():
    for text in _blocks("server/static/api.md").values():
        assert "review_subject" in text
        assert "PRs are optional" in text
        assert "after merge" not in text


def test_계약_블록은_docs_정본과도_일치한다():
    """RPXH(M53PBDWJ-RPXH) — 반복 지적(계약 문서 동기화 누락)의 테스트 전환.
    최신 계약 버전 마커는 정본(docs)·서버·스킬 사본 모두에 언급돼야 한다."""
    latest = MARKERS[-1]
    for f in ("docs/API.md", "server/static/api.md", "skills/tt/references/api.md",
              "skills/tt/SKILL.md"):
        s = (ROOT / f).read_text()
        assert latest in s, f"{f}: 최신 계약 {latest} 언급 없음 — 문서 동기화 누락(리뷰 지적 반복 유형)"


# --- 9WWK/CJS6 문서 패리티 — 엔드포인트·응답 필드 스냅샷 ---
# 새 엔드포인트/응답 필드 추가 시 이 목록과 server/static/api.md·skills/tt/references/api.md를
# 함께 갱신한다(허용 목록으로 예외 관리 — 비워두면 전부 문서화가 기본).
OPENAPI_PATHS = (
    "/agents", "/agents/active", "/agents/{name}", "/agents/{runner_name}/pending",
    "/events", "/events/stream", "/health", "/issues", "/issues/{issue_id}",
    "/issues/{issue_id}/archive", "/issues/{issue_id}/claim", "/issues/{issue_id}/claim-review",
    "/issues/{issue_id}/comments", "/issues/{issue_id}/dispatch", "/issues/{issue_id}/dispatches",
    "/issues/{issue_id}/dispatches/{dispatch_id}/progress", "/issues/{issue_id}/lease",
    "/issues/{issue_id}/ping", "/issues/{issue_id}/tree", "/issues/{issue_id}/verify",
    "/issues/{issue_id}/why-blocked", "/maintenance/archive-sweep", "/messages",
    "/messages/unread", "/messages/{mid}/read", "/pull", "/rejections/summary",
    "/settings/{key}", "/work-contract",
)
ISSUE_FIELDS = (
    "acceptance", "approval_path", "archived", "assignee", "blocked_default",
    "blocked_detail", "blocked_notified_at", "blocked_options", "blocked_question",
    "body", "brief_forbidden", "brief_scope", "brief_timebox", "brief_verify",
    "children", "comments", "completed_at", "completion_report", "created_at",
    "delayed", "delivery_receipt", "evidence_after_comment_id", "execution_attempt",
    "heartbeat_at", "id", "labels", "lease_by", "lease_expires", "nominee",
    "parent_id", "premise_rework", "priority", "review_subject", "reviewer",
    "started_at", "state", "submission_revision", "title", "todo_since",
    "updated_at", "verification_status", "verified", "verified_at",
    "verified_evidence", "version", "waiting_actor", "waiting_for", "work_contract",
)
_PATH_ALLOWLIST = set()
_FIELD_ALLOWLIST = set()


# 문서는 {id} 축약 템플릿을 쓴다 — openapi의 매개변수명을 정규화해 비교
_PATH_ALIAS = {"{issue_id}": "{id}", "{mid}": "{id}", "{dispatch_id}": "{did}"}


def _doc_path(p):
    for a, b in _PATH_ALIAS.items():
        p = p.replace(a, b)
    return p


def test_openapi_경로는_두_api문서에_모두_문서화돼_있다():
    for f in ("server/static/api.md", "skills/tt/references/api.md"):
        s = (ROOT / f).read_text()
        missing = [p for p in OPENAPI_PATHS
                   if _doc_path(p) not in s and p not in _PATH_ALLOWLIST]
        assert not missing, f"{f}: 미문서화 엔드포인트 {missing} — 문서 보강 또는 allowlist"


def test_issue_응답_필드는_두_api문서에_모두_문서화돼_있다():
    for f in ("server/static/api.md", "skills/tt/references/api.md"):
        s = (ROOT / f).read_text()
        missing = [k for k in ISSUE_FIELDS if k not in s and k not in _FIELD_ALLOWLIST]
        assert not missing, f"{f}: 미문서화 응답 필드 {missing} — 문서 보강 또는 allowlist"


def test_두_api문서는_바이트_동일하다():
    """스킬 사본은 번들 스냅숏 — 계약 블록뿐 아니라 전체가 서버 정본과 같아야 한다.
    (45줄 드리프트로 에이전트가 구 정책·이벤트 절을 읽는 사고 방지)"""
    src = (ROOT / "server/static/api.md").read_text()
    ref = (ROOT / "skills/tt/references/api.md").read_text()
    assert src == ref, "skills/tt/references/api.md를 server/static/api.md로 재동기화할 것"
