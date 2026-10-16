#!/usr/bin/env python3
"""타이브레이커 케이스 로그 검증 (TT M4NDBRX3-XVY7, docs/breaker-case-log.md).

스키마 검증 + 사유코드 사전(server/failure_rules.py FAILURE_RULES) 대조 +
(trigger_seq, card) dedup 검출. 읽기 전용 — 로그를 수정하지 않는다.

사용: python3 scripts/tt-case-log-check.py [PATH]
      기본 PATH = $TT_CASE_LOG 또는 ~/tt-cases/breakers.jsonl
exit: 0=OK, 1=스키마/사전 오류, 2=파일·사용 오류
"""
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
from failure_rules import FAILURE_RULES  # noqa: E402  (읽기 전용 import — 서버 코드 무변경)

REASON_CODES = {r["code"] for r in FAILURE_RULES}
DETECTOR_RE = re.compile(r"^(probe|daemon|agent:[^:\s]+|human:[^:\s]+)$")
DECIDERS = {"issuer", "supervisor-agent", "llm-advisory"}
REQUIRED = ("trigger_seq", "card", "reason_code", "detector", "decider", "decision")


def validate(records):
    """레코드 리스트 → 오류 문자열 리스트(빈 리스트=통과)."""
    errors, seen = [], set()
    for n, rec in enumerate(records, 1):
        if not isinstance(rec, dict):
            errors.append(f"{n}행: JSON object 아님")
            continue
        for f in REQUIRED:
            if f not in rec:
                errors.append(f"{n}행: 필수 필드 누락 '{f}'")
        if errors:
            continue
        if not isinstance(rec["trigger_seq"], int) or isinstance(rec["trigger_seq"], bool):
            errors.append(f"{n}행: trigger_seq는 int")
        if not isinstance(rec["card"], str) or not rec["card"].strip():
            errors.append(f"{n}행: card는 비어 있지 않은 str")
        if rec["reason_code"] not in REASON_CODES:
            errors.append(f"{n}행: reason_code '{rec['reason_code']}' 없음 — "
                          f"FAILURE_RULES 코드 재사용(신설 금지)")
        if not isinstance(rec["detector"], str) or not DETECTOR_RE.match(rec["detector"]):
            errors.append(f"{n}행: detector는 probe|daemon|agent:<이름>|human:<신원>")
        if rec["decider"] not in DECIDERS:
            errors.append(f"{n}행: decider는 issuer|supervisor-agent|llm-advisory")
        if not isinstance(rec["decision"], str) or not rec["decision"].strip():
            errors.append(f"{n}행: decision 비어 있음(결정 한 줄+근거 필수)")
        key = (rec["trigger_seq"], rec["card"])
        if key in seen:
            errors.append(f"{n}행: 중복 — (trigger_seq, card)={key} 기존재")
        seen.add(key)
        fc = rec.get("followup_card")
        if fc is not None and (not isinstance(fc, str) or not fc.strip()):
            errors.append(f"{n}행: followup_card는 카드 ID str 또는 null")
    return errors


def main(argv):
    path = Path(argv[1] if len(argv) > 1 else
                os.environ.get("TT_CASE_LOG", "~/tt-cases/breakers.jsonl")).expanduser()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        print(f"파일 읽기 실패: {e}", file=sys.stderr)
        return 2
    records = []
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as e:
            print(f"{n}행: JSON 파싱 실패 — {e}", file=sys.stderr)
            return 1
    errors = validate(records)
    if errors:
        for e in errors:
            print(e, file=sys.stderr)
        return 1
    print(f"OK — {len(records)}건 (중복 0, 사유코드 전건 대조 통과)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
