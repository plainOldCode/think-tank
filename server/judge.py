"""판단기 인터페이스 (probe-restructure-design.md §5.2, gate-failure-rules.md §5).

R3(TT M4K87YWR-ZXBJ): C1/C2/S1/S2 연결 지점 + §8.1 판단 로그. 이 카드는
인터페이스+배선까지만 — 실제 모델 구현·그림자 런타임은 후속 카드.

불변 원칙(§5.2):
- 단조성: Proposal.output은 hold/escalate/annotate/rank뿐 — allow·merge는
  타입 수준에서 불가능하다(규칙만 낸다).
- fail-to-rules: 판단기 오류/무응답 시 규칙 기본 동작 그대로 + fallback count.
  재시도는 호출당 1회 한정(사이클 정지 방지).
- 기록 우선: 판단기가 개입한 모든 결정은 §8.1 스키마 JSONL 로그. 미구성(기본)
  판단기는 개입 자체가 없으므로 기록도 없다 — 규칙 동작은 변하지 않는다.
"""
import hashlib
import json
import os
import threading
import time
import uuid

# §5.2 — 판단기 출력 타입 전체. advisory 지점(C1/C2/S1/S2)에서 쓰는 것은
# annotate/rank뿐이지만 타입 집합은 §5.2 정의를 그대로 둔다.
OUTPUT_TYPES = ("hold", "escalate", "annotate", "rank")

LOG_SCHEMA = ("decision_id", "cycle_id", "ts", "point", "card", "pr", "input_hash",
              "judge", "mode", "output", "rule_output", "gate", "latency_ms",
              "fallback", "trace_id")

_JUDGE = [None]
_LOCK = threading.Lock()


class Proposal:
    """판단기 제안 — output은 §5.2 4값만 허용(단조성)."""

    def __init__(self, output, value=None, trace_id=None):
        if output not in OUTPUT_TYPES:
            raise ValueError(f"판단기 출력 {output!r} — §5.2 타입 집합 밖(allow/merge 불가)")
        self.output = output
        self.value = value
        self.trace_id = trace_id


class BaseJudge:
    """주입 가능한 판단기. propose는 Proposal 또는 None(무응답)을 반환한다.
    예외 = 오류(fail-to-rules). 구현은 후속 카드(LLM 클라이언트+그림자 런타임)."""

    name = "noop"

    def propose(self, point, view):
        raise NotImplementedError


def get_judge():
    return _JUDGE[0]


def set_judge(j):
    """테스트/후속 카드용 주입 지점. None이면 미구성(규칙만)."""
    _JUDGE[0] = j


def log_path():
    return os.environ.get("TT_JUDGE_LOG", "judge-log.jsonl")


def input_hash(view):
    """정규화 입력의 sha256 — 동일 입력 재판정 dedup(C1)과 감사 대조용."""
    norm = json.dumps(view.get("input"), ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(norm.encode()).hexdigest()


class JudgeLog:
    """§8.1 판단 로그 — JSONL. 저장 위치는 열린 질문 Q2(§10)의 구현 카드 결정 사항:
    서버 스키마 변경 없음 + 파일 1개(TT_JUDGE_LOG, 기본 judge-log.jsonl)."""

    def __init__(self, path=None):
        self.path = path or log_path()

    def record(self, *, point, judge, mode, output, rule_output, gate,
               cycle_id=None, card=None, pr=None, input_hash_=None,
               latency_ms=None, fallback=False, trace_id=None):
        rec = {"decision_id": str(uuid.uuid4()),
               "cycle_id": cycle_id,
               "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
               "point": point, "card": card, "pr": pr,
               "input_hash": input_hash_,
               "judge": judge, "mode": mode, "output": output,
               "rule_output": rule_output, "gate": gate,
               "latency_ms": latency_ms, "fallback": bool(fallback),
               "trace_id": trace_id}
        with _LOCK:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return rec

    def seen(self, input_hash_):
        """같은 입력 판정 이력 — C1 dedup(로그 재집계 방지)."""
        if not os.path.exists(self.path):
            return False
        try:
            with open(self.path, encoding="utf-8") as f:
                return any(json.dumps({"h": input_hash_})["h"] in line
                           for line in f if input_hash_ in line)
        except OSError:
            return False

    @staticmethod
    def counts(path=None):
        """count 기반 평가(§7) — 제안 수/모드별/fallback/무응답. accuracy류 지표 제외."""
        path = path or log_path()
        out = {"total": 0, "by_point": {}, "by_output": {}, "by_mode": {},
               "fallback": 0, "no_response": 0}
        if not os.path.exists(path):
            return out
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                out["total"] += 1
                out["by_point"][r["point"]] = out["by_point"].get(r["point"], 0) + 1
                out["by_output"][r["output"]] = out["by_output"].get(r["output"], 0) + 1
                out["by_mode"][r["mode"]] = out["by_mode"].get(r["mode"], 0) + 1
                if r.get("fallback"):
                    out["fallback"] += 1
                if r.get("output") == "no_response":
                    out["no_response"] += 1
        return out


def run_advisory(point, view, rule_output, *, log=None, judge=None, cycle_id=None):
    """advisory 지점 공통 실행 — 미구성이면 None(규칙만, 기록 없음).

    구성된 판단기: propose → Proposal이면 §8.1 기록 후 반환(shadow/advisory —
    호출자는 출력을 동작에 직결하지 않는다). 오류·무응답·비정상 출력이면
    재시도 1회까지 시도 후 규칙 기본(None) + fallback 기록."""
    j = judge if judge is not None else get_judge()
    if j is None:
        return None
    log = log or JudgeLog()
    ih = input_hash(view)
    t0 = time.monotonic()
    result, err = None, True
    for attempt in (1, 2):  # 재시도는 1회 한정(§5.4) — 사이클 정지 방지
        try:
            result = j.propose(point, view)
            err = False
            break
        except Exception:
            result = None
    latency = int((time.monotonic() - t0) * 1000)
    valid = (not err and isinstance(result, Proposal)
             and result.output in ("annotate", "rank"))
    if not valid:
        log.record(point=point, judge=getattr(j, "name", "judge"), mode="shadow",
                   output="no_response", rule_output=rule_output, gate="deny",
                   cycle_id=cycle_id, card=view.get("card"), pr=view.get("pr"),
                   input_hash_=ih, latency_ms=latency, fallback=True,
                   trace_id=getattr(result, "trace_id", None) if result else None)
        return None
    log.record(point=point, judge=getattr(j, "name", "judge"), mode="shadow",
               output=result.output, rule_output=rule_output, gate="allow",
               cycle_id=cycle_id, card=view.get("card"), pr=view.get("pr"),
               input_hash_=ih, latency_ms=latency, fallback=False,
               trace_id=result.trace_id)
    return result
