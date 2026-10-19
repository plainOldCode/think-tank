"""판단 로그 (gate-failure-rules.md §8.1).

U5(M5A4HZAT-RNT4, 사용자 결정 A 2026-10-20) — 미구성 판단기 골격(제안 클래스·
주입 지점·입력 해시·이력 조회·집계·advisory 실행기)은 운영 코드에서 제거됐다.
골격은 research/judge-c1 브랜치와 문서 부록으로 보존한다. 남는 것은
7AMG 판단 로그(JudgeLog.record·회전)와 decide action 1행 기록(log_decide_actions),
90Y9 review_repeat_warn(core)뿐이다.
"""
import hashlib
import json
import os
import threading
import time
import uuid

from failure_rules import RULE_VERSION as _RULE_VERSION
from ontology.schema import SCHEMA_VERSION as _ENTITY_SCHEMA_VERSION

LOG_SCHEMA = ("decision_id", "cycle_id", "ts", "point", "card", "pr", "input_hash",
              "judge", "mode", "output", "rule_output", "gate", "latency_ms",
              "fallback", "trace_id",
              # 7AMG(M58SPSBY-7AMG) — 결정 근거·신뢰도·버전. evidence는 id만.
              "evidence", "confidence", "rule_version", "schema_version")

# 7AMG — 판단 로그 버전 상속(G1DD): schema_version은 온톨로지 정본 스키마,
# rule_version은 게이트 실패 규칙표의 정본 상수에서 온다(단일 출처).
SCHEMA_VERSION = _ENTITY_SCHEMA_VERSION
RULE_VERSION = _RULE_VERSION
CONFIDENCE_LEVELS = ("high", "med", "low")
# evidence 항목 허용 키 — id/code만(본문·원문 금지, TV Q4)
EVIDENCE_KEYS = ("card", "agent", "pr", "comment_id", "dispatch_id", "sha8", "reason_code")
# 보존 정책: TT_JUDGE_LOG_MAX_BYTES(기본 5MB) 초과 시 <path>.1로 1세대 회전.
# .1은 다음 회전에 덮어쓰인다(최대 2파일, 상한 2×max).
JUDGE_LOG_MAX_BYTES = 5_000_000

_LOCK = threading.Lock()


def _clean_evidence(evidence):
    """7AMG — evidence는 id/code만(TV Q4). 허용 키 밖 항목은 조용히 버린다.

    스칼라 값만 남기고 문자열은 128자로 절단 — 본문 인용이 스며들지 않게 한다."""
    if not evidence:
        return []
    clean = []
    for item in evidence:
        if not isinstance(item, dict):
            continue
        kept = {k: (str(v)[:128] if isinstance(v, str) else v)
                for k, v in item.items()
                if k in EVIDENCE_KEYS and isinstance(v, (str, int))}
        if kept:
            clean.append(kept)
    return clean


def log_decide_actions(snap, actions, *, log=None):
    """7AMG — decide가 낸 action마다 1행(shadow). 동작 불변: 기록만 하고
    반환값·예외를 decide로 새지 않는다. 결정 근거는 evidence(id만)로 남긴다."""
    lg = log if log is not None else JudgeLog()
    for a in actions or []:
        if not isinstance(a, dict):
            continue
        evidence = []
        if a.get("issue"):
            evidence.append({"card": a["issue"]})
        if a.get("agent") and a.get("agent") != "probe":
            evidence.append({"agent": a["agent"]})
        if a.get("pr"):
            evidence.append({"pr": a["pr"]})
        if a.get("retry") is not None:
            evidence.append({"reason_code": f"retry={a['retry']}"})
        if a.get("reason"):
            evidence.append({"reason_code": str(a["reason"])})
        try:
            lg.record(point="decide", judge="probe", mode="shadow",
                      output=str(a.get("action") or ""), rule_output=str(a.get("action") or ""),
                      gate="allow", cycle_id=snap.get("cycle_id"), card=a.get("issue"),
                      pr=a.get("pr"), confidence="high", rule_version=RULE_VERSION,
                      schema_version=SCHEMA_VERSION, evidence=evidence)
        except Exception as e:
            # 기록 실패가 decide를 취소하지 않는다(§5.2 기록 우선).
            print(time.strftime("%F %T"), f"decide 로그 실패({e}) — 생략", flush=True)


def log_path():
    return os.environ.get("TT_JUDGE_LOG", "judge-log.jsonl")


class JudgeLog:
    """§8.1 판단 로그 — JSONL. 저장 위치는 열린 질문 Q2(§10)의 구현 카드 결정 사항:
    서버 스키마 변경 없음 + 파일 1개(TT_JUDGE_LOG, 기본 judge-log.jsonl).
    쓰기 장애는 규칙 동작을 중단시키지 않는다(기록 우선, 규칙 우선 — 리뷰 R5).

    보존 정책(7AMG): TT_JUDGE_LOG_MAX_BYTES(기본 5MB) 초과 시 <path>.1로 1세대
    회전 — 최대 2파일(본체+직전 세대), 상한 2×max. 재시작 없이 판단 가능."""

    def __init__(self, path=None):
        self.path = path or log_path()
        try:
            d = os.path.dirname(self.path)
            if d:
                os.makedirs(d, exist_ok=True)
        except OSError:
            pass

    def record(self, *, point, judge, mode, output, rule_output, gate,
               cycle_id=None, card=None, pr=None, input_hash_=None,
               latency_ms=None, fallback=False, trace_id=None,
               evidence=None, confidence=None, rule_version=None,
               schema_version=None):
        if confidence is not None and confidence not in CONFIDENCE_LEVELS:
            # 7AMG 수용기준 4 — enum 밖 신뢰도는 기록하지 않고 거부한다(수치 금지).
            raise ValueError(f"confidence {confidence!r} — {CONFIDENCE_LEVELS} 밖")
        clean_ev = _clean_evidence(evidence)
        self._rotate()
        rec = {"decision_id": str(uuid.uuid4()),
               "cycle_id": cycle_id,
               "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
               "point": point, "card": card, "pr": pr,
               "input_hash": input_hash_,
               "judge": judge, "mode": mode, "output": output,
               "rule_output": rule_output, "gate": gate,
               "latency_ms": latency_ms, "fallback": bool(fallback),
               "trace_id": trace_id,
               "evidence": clean_ev, "confidence": confidence,
               "rule_version": rule_version, "schema_version": schema_version}
        try:
            with _LOCK:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError as e:
            # 기록 실패가 규칙 집행을 취소하지 않는다(리뷰 R5) — 관측만 남긴다.
            print(time.strftime("%F %T"), f"판단 로그 쓰기 실패({e}) — 기록 생략", flush=True)
            return None
        return rec

    def _rotate(self):
        """보존 정책(7AMG) — 상한 초과 시 1세대 회전. 장애는 기록을 막지 않는다."""
        limit = JUDGE_LOG_MAX_BYTES
        try:
            limit = int(os.environ.get("TT_JUDGE_LOG_MAX_BYTES", str(JUDGE_LOG_MAX_BYTES)))
        except ValueError:
            pass
        try:
            if os.path.exists(self.path) and os.path.getsize(self.path) > limit:
                os.replace(self.path, self.path + ".1")
        except OSError:
            pass


