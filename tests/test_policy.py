"""TT 개선#3b — busy/eligible 단일 정책 모듈.

기록 카드 상태·작업 lease·리뷰 점유·runner 실행을 server/policy.py 한곳에서
판정한다. probe(자동 배정)·서버 /pull·러너 수령이 같은 reason code를 공유한다.
"""
import policy


def _i(**kw):
    base = {"id": "X", "state": "todo", "archived": 0, "assignee": "", "reviewer": "",
            "lease_by": "", "lease_expires": None, "labels": ["auto"],
            "dispatches": 0, "execution_attempt": 0}
    base.update(kw)
    return base


NOW = "2026-10-09T12:00:00+0900"


def test_terminal_states_are_busy():
    for st in ("done", "cancelled"):
        assert policy.busy_reason(_i(state=st), now=NOW) == "state:terminal"
    assert policy.busy_reason(_i(archived=1), now=NOW) == "archived"


def test_non_todo_states_block_new_work():
    assert policy.busy_reason(_i(state="backlog"), now=NOW) == "state:backlog"
    assert policy.busy_reason(_i(state="blocked"), now=NOW) == "state:blocked"
    assert policy.busy_reason(_i(state="review"), now=NOW) == "state:review"


def test_active_lease_blocks_expired_lease_does_not():
    held = _i(state="in_progress", assignee="a@t", lease_by="a@t",
              lease_expires="2026-10-09T13:00:00+0900")
    # runner 실행 중 = 작업 lease 보유와 동일 차원 — 단일 코드 lease_held
    assert policy.busy_reason(held, now=NOW) == "lease_held"
    expired = _i(state="in_progress", assignee="a@t", lease_by="a@t",
                 lease_expires="2026-10-09T11:00:00+0900")
    assert policy.busy_reason(expired, now=NOW) is None
    # 리뷰 점유(리뷰어 lease 유효) — review 카드지만 lease가 리뷰 점유의 실체
    rev = _i(state="review", reviewer="r@t", lease_by="r@t",
             lease_expires="2026-10-09T13:00:00+0900")
    assert policy.busy_reason(rev, now=NOW) == "state:review"


def test_budget_blocked_codes():
    assert policy.busy_reason(_i(dispatches=2), now=NOW) == "budget:dispatch-tries>=2"
    assert policy.busy_reason(_i(execution_attempt=2), now=NOW) == "budget:attempt>=2"
    assert policy.busy_reason(_i(dispatches=1, execution_attempt=1), now=NOW) is None


def test_not_auto_only_when_auto_mode():
    plain = _i(labels=[])
    assert policy.busy_reason(plain, now=NOW, auto=True) == "not_auto"
    assert policy.busy_reason(plain, now=NOW, auto=False) is None


def test_eligible_tuple_shape():
    ok, reason = policy.eligible(_i(), now=NOW, auto=True)
    assert ok is True and reason is None
    ok, reason = policy.eligible(_i(state="done"), now=NOW, auto=True)
    assert ok is False and reason == "state:terminal"


def test_pull_uses_policy_as_authoritative_filter(client):
    """pull SQL 사전필터 뒤에 policy 재판정 — busy면 후보를 건너뛴다."""
    from app import create_app
    # lease 유효 카드는 pull 대상에서 제외(기존 동작 유지) — 만료 lease는 재수령
    a = client.post("/issues", json={"title": "t1", "acceptance": "기준"}).json()
    r = client.post("/pull", json={"agent": "w1@t", "hours": 6})
    assert r.json()["id"] == a["id"]
    # w1이 lease 보유 → 한도 내 두 번째 pull은 후보 없음
    r2 = client.post("/pull", json={"agent": "w2@t", "hours": 6})
    assert r2.status_code == 200 and r2.json() is None


def test_probe_logs_skip_reasons(capsys):
    """probe decide가 skip 사유를 남기고 run_once 로그에 찍힌다(TT 개선#3b)."""
    import probe.core as core
    issues = [
        _i(id="BUSY1", state="in_progress", assignee="a@t", lease_by="a@t",
           lease_expires="2026-10-09T13:00:00+0900"),
        _i(id="BUDGET1", dispatches=2),
        _i(id="PLAIN1", labels=[]),
    ]
    snap = {"auto": True, "now": NOW, "agents": [], "issues": issues, "prs": []}
    acts = core.decide(snap)
    skips = snap.get("probe_skips") or {}
    assert skips.get("BUSY1") == "lease_held"
    assert skips.get("BUDGET1") == "budget:dispatch-tries>=2"
    assert skips.get("PLAIN1") == "not_auto"
    # work 배정 액션은 busy 카드를 향하지 않는다(needs-human 공지는 예외)
    assert not [a for a in acts if a.get("action") == "work"
                and a.get("issue") in ("BUSY1", "BUDGET1", "PLAIN1")]
