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

from failure_rules import RULE_VERSION as _RULE_VERSION
from ontology.schema import SCHEMA_VERSION as _ENTITY_SCHEMA_VERSION

# §5.2 — 판단기 출력 타입 전체. advisory 지점(C1/C2/S1/S2)에서 쓰는 것은
# annotate/rank뿐이지만 타입 집합은 §5.2 정의를 그대로 둔다.
OUTPUT_TYPES = ("hold", "escalate", "annotate", "rank")

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

_JUDGE = [None]
_LOCK = threading.Lock()


def sanitize(text):
    """advisory 텍스트에서 마커 시퀀스를 무력화 — 요약이 기존 마커를 인용해도
    제어 정보로 재해석되지 않는다(§5.2 원칙 3, 리뷰 R2: escaping 경계)."""
    return str(text).replace("[", "［").replace("]", "］")


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


def input_hash(point, view):
    """(지점, 카드, 정규화 입력)의 sha256 — dedup 키가 카드·지점에 스코프된다(리뷰 R9:
    서로 다른 카드의 동일 스니펫을 하나로 지우지 않게)."""
    norm = json.dumps({"point": point, "card": view.get("card"),
                       "input": view.get("input")},
                      ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(norm.encode()).hexdigest()


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

    def seen(self, card, point, input_hash_):
        """(카드, 지점, 입력 해시) 판정 이력 — C1 dedup(재시작 후에도). 리뷰 R9:
        스코프는 카드+지점+해시 조합. 파일 오류 시 False(보수적으로 재판정)."""
        if not os.path.exists(self.path):
            return False
        try:
            with open(self.path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if (r.get("card") == card and r.get("point") == point
                            and r.get("input_hash") == input_hash_):
                        return True
        except OSError:
            return False
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
                otype = (r.get("output") or "").split(":", 1)[0].strip()
                out["by_output"][otype] = out["by_output"].get(otype, 0) + 1
                out["by_mode"][r["mode"]] = out["by_mode"].get(r["mode"], 0) + 1
                if r.get("fallback"):
                    out["fallback"] += 1
                if (r.get("output") or "").startswith("no_response"):
                    out["no_response"] += 1
        return out


def run_advisory(point, view, rule_output, *, log=None, judge=None, cycle_id=None):
    """advisory 지점 공통 실행 — 미구성이면 None(규칙만, 기록 없음).

    구성된 판단기: propose → Proposal이면 §8.1 기록 후 반환(shadow/advisory —
    호출자는 출력을 동작에 직결하지 않는다). 오류·무응답·비정상 출력이면
    재시도 1회까지 시도 후 규칙 기본(None) + fallback 기록. 반환 Proposal에는
    decision_id가 붙는다 — 기존 파서와 충돌하지 않는 [judge-note <id>] 주석
    식별자로 쓴다(§5.2 원칙 3, 리뷰 R2)."""
    j = judge if judge is not None else get_judge()
    if j is None:
        return None
    log = log or JudgeLog()
    ih = input_hash(point, view)
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
    value = "" if result.value is None else str(result.value)
    rec = log.record(point=point, judge=getattr(j, "name", "judge"), mode="shadow",
                     output=f"{result.output}: {value[:200]}", rule_output=rule_output,
                     gate="allow", cycle_id=cycle_id, card=view.get("card"),
                     pr=view.get("pr"), input_hash_=ih, latency_ms=latency,
                     fallback=False, trace_id=result.trace_id)
    if rec is None:
        # 기록 우선(§5.2 원칙 3) — 로그에 남지 않은 출력은 게시하지 않는다(리뷰 R5).
        # 규칙 기본 동작으로 폴백한다.
        return None
    result.decision_id = rec["decision_id"]
    return result
