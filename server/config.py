"""환경설정·상수 — think-tank 서버.

env는 호출 시점 읽기(lazy): TT_DONE_GATE 등은 테스트가 env 격리로 경계를 검증하므로
모듈 임포트 시 스냅샷하지 않는다(재시작 없이도 반영).
"""
import os
import pathlib
import re

import db as dbmod

CLI_PATH = pathlib.Path(__file__).resolve().parent.parent / "cli" / "tt"

STATE_ENUM = sorted(dbmod.STATES)

# done≠verified 게이트 모드 (M3BZV172-9F0S A): gate(기본, 증거 없으면 review 강등) |
# warn(경고 댓글만, done 허용) | off. TT_NOTIFY_BASE: notify agent base_url 접두어 —
# 상대경로(/hook)를 실 Telegram 알림 주입 경로(Hermes 어댑터)로 해석할 때 쓴다.
DONE_GATE_MSG = ("done 증거 없음 또는 실패/미확정 보고 — 현재 회차의 성공 결과가 필요합니다. "
                 "tt done {iid} --report report.json 또는 tt verify {iid} --report report.json. "
                 "재작업: tt edit {iid} --state todo 후 claim. force_done/close는 승인 예외입니다.")

ISSUE_ID_RE = re.compile(r"\b[0-9A-HJKMNP-TV-Z]{8}-[0-9A-HJKMNP-TV-Z]{4}\b")

# blocked 해제 기준 문구 (M3BZS1FS-5722 ② 왜-blocked 투영용; 자동 생성 아님)
WAIT_CRITERIA = {
    "human": "책임 액터(waiting_actor)의 결정·코멘트가 있어야 재개 가능",
    "gate": "'승인'/'approve' dispatch(파괴적 패턴 게이트 해제)가 있어야 재개 가능",
    "dependency": "blocked_detail에 명시한 의존 이슈가 모두 done/cancelled이어야 재개 가능",
    "external": "blocked_detail에 명시한 외부 조건이 충족되어야 재개 가능",
}

RUN_STATES = {"queued", "running", "stalled", "finished", "failed"}
ACTIVE_RUN_STATES = ("queued", "running", "stalled")  # 활성 = 미종료·실행정보 있음


def done_gate() -> str:
    return os.environ.get("TT_DONE_GATE", "gate").lower()


def notify_base() -> str:
    return os.environ.get("TT_NOTIFY_BASE", "").rstrip("/")


def max_leases() -> int:
    """활성 lease 보유 한도(claim/pull 공통) — TT_MAX_LEASES(기본 2). 호출 시점 읽기:
    재시작 없이도 반영 가능하고 테스트가 env 격리로 경계를 검증한다. 1 미만 값은 2로 폴백."""
    try:
        n = int(os.environ.get("TT_MAX_LEASES", "2"))
    except ValueError:
        return 2
    return n if n >= 1 else 2


def probe_interval() -> int:
    """probe 내장 루프 주기(초) — TT_PROBE_INTERVAL. 0/unset/파싱불가=비활성(기본 off).
    서버 임베드 모드 전용(M3ZW8E8A-ZK3G); standalone dispatchd는 TT_DISPATCH_INTERVAL 사용."""
    try:
        return max(0, int(os.environ.get("TT_PROBE_INTERVAL", "0")))
    except ValueError:
        return 0


def parse_iso(s):
    from datetime import datetime
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S%z")
    except (ValueError, TypeError):
        try:
            return datetime.fromisoformat(s)
        except (ValueError, TypeError):
            return None
