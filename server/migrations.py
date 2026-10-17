"""DX88(M5283J3A-DX88) — 기존 DB의 비정규화·정크 assignee/lease_by 정규화 마이그레이션.

정규화 규칙(수령 관문 _normalize_claim_agent와 동일 판정):
- 등록 에이전트 이름·Z1HP 별칭으로 해석되는 값 → 등록명 치환 (예: codex@m2pro → codex)
- 빈 값·숫자만 있는 값 → 해제(assignee=""). in_progress 카드는 좀비(회수 불가)가 되지
  않게 todo로 재큐하고 lease를 해제한다 — review 상태는 상태 유지(리뷰어 재선정은 probe 몫)
- 해석 불가한 호스트 표기(legacy@old-host 등)는 보존 — 감사 가치 유지,
  게이트(TT_REQUIRE_KNOWN_AGENT) 활성 후 새 수령만 차단된다

멱등: 정규명·해제된 값은 재실행에서 변화 없음. 이벤트 로그를 쓰지 않는다 —
대량 rewrite가 W098류 이벤트 기반 지표를 오염하지 않게 함(스크립트 통계로 감사).
"""
import json

import db as dbmod
import service


def _plan(c, value):
    """단일 값 판정 → (새 값, 행동). 행동: None|'normalize'|'clear'."""
    v = value or ""
    s = v.strip()
    if not v:
        return v, None
    resolved = service.resolve_agent_identity(c, s)
    if resolved:
        return (resolved, "normalize" if resolved != v else None)
    if not s or s.isdigit():
        return "", "clear"
    if s != v:
        return s, "normalize"
    return v, None


def migrate_assignees(c, apply: bool = True) -> dict:
    """issues 전수 순회 — assignee/lease_by 정규화. apply=False면 통계만(dry-run).
    apply=True면 호출 연결에 커밋까지 수행. 반환 통계는 실행 횟수와 무관하게 누적 감소."""
    stats = {"assignee_normalized": 0, "assignee_cleared": 0,
             "lease_by_normalized": 0, "lease_by_cleared": 0,
             "reviewer_normalized": 0, "reviewer_cleared": 0,
             "nominee_normalized": 0, "nominee_cleared": 0}
    rows = c.execute("SELECT id, state, assignee, lease_by, reviewer, nominee FROM issues").fetchall()
    for r in rows:
        sets = {}
        na, act_a = _plan(c, r["assignee"])
        if act_a == "normalize":
            sets["assignee"] = na
            stats["assignee_normalized"] += 1
        elif act_a == "clear":
            sets["assignee"] = ""
            stats["assignee_cleared"] += 1
            if r["state"] == "in_progress":
                # 좀비 재큐 — assignee 없는 in_progress는 claim/pull 어디로도 회수 불가
                sets.update({"state": "todo", "started_at": None, "todo_since": dbmod.now()})
        nl, act_l = _plan(c, r["lease_by"])
        if act_l == "normalize":
            sets["lease_by"] = nl
            stats["lease_by_normalized"] += 1
        elif act_l == "clear":
            sets.update({"lease_by": "", "lease_expires": None})
            stats["lease_by_cleared"] += 1
        # R7Y4(M53PBDC0-R7Y4): reviewer·nominee도 같은 규칙 — 칸 누락으로 비정규화 값 잔존
        for col, key in (("reviewer", "reviewer_normalized"),
                         ("nominee", "nominee_normalized")):
            nv, act_n = _plan(c, r[col])
            if act_n == "normalize" and r[col] != nv:
                sets[col] = nv
                stats[key] += 1
            elif act_n == "clear":
                sets[col] = ""
                stats[key.replace("normalized", "cleared")] = \
                    stats.get(key.replace("normalized", "cleared"), 0) + 1
        if "state" in sets:
            # 재큐된 카드에 잔여 lease가 남지 않게 — 정규화 결과보다 우선
            sets.update({"lease_by": "", "lease_expires": None})
        if sets and apply:
            sets["updated_at"] = dbmod.now()
            cols = ", ".join(f"{k}=?" for k in sets)
            c.execute(f"UPDATE issues SET {cols} WHERE id=?", (*sets.values(), r["id"]))
    if apply:
        c.commit()
    return stats
