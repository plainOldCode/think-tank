"""판단 로그 테스트 (§8.1 — U5 M5A4HZAT-RNT4 후 잔여).

U5(사용자 결정 A, 2026-10-20)로 판단기 골격(Proposal/BaseJudge/get·set_judge/
input_hash/seen/counts/run_advisory)은 운영 코드에서 제거됐다 — 골격 테스트는
research/judge-c1 브랜치로. 남는 것: JudgeLog.record의 §8.1 스키마·회전과
7AMG decide action 1행 기록(test_7amg.py), 90Y9 review_repeat_warn(test_90y9.py).
"""
import json
import os

import pytest

import judge

LOG_SCHEMA = ("decision_id", "cycle_id", "ts", "point", "card", "pr", "input_hash",
              "judge", "mode", "output", "rule_output", "gate", "latency_ms",
              "fallback", "trace_id",
              "evidence", "confidence", "rule_version", "schema_version")


@pytest.fixture
def log_path(tmp_path):
    return str(tmp_path / "judge-log.jsonl")


def test_판단_기록은_스키마_필드를_갖는다(log_path):
    rec = judge.JudgeLog(log_path).record(
        point="decide", judge="probe", mode="shadow", output="tt-review",
        rule_output="tt-review", gate="allow", cycle_id="cyc-1", card="M1",
        pr=9, confidence="high", rule_version="gf-r1", schema_version=2)
    assert set(rec) == set(LOG_SCHEMA)
    assert rec["point"] == "decide" and rec["mode"] == "shadow"
    assert rec["output"] == "tt-review" and rec["fallback"] is False
    assert rec["rule_version"] == "gf-r1" and rec["schema_version"] == 2
    assert json.loads(open(log_path, encoding="utf-8").read())["decision_id"] \
        == rec["decision_id"]


def test_confidence_enum_밖은_거부한다(log_path):
    # 7AMG 수용기준 4 — 수치·enum 밖 신뢰도는 기록하지 않고 거부
    with pytest.raises(ValueError):
        judge.JudgeLog(log_path).record(
            point="decide", judge="probe", mode="shadow", output="x",
            rule_output="x", gate="allow", confidence=0.9)
    assert not os.path.exists(log_path)


def test_상한_초과시_1세대_회전한다(log_path, monkeypatch):
    # 7AMG 보존 정책 — max 초과 시 <path>.1로 교체, 본체는 새 파일
    lg = judge.JudgeLog(log_path)
    lg.record(point="decide", judge="probe", mode="shadow", output="x",
              rule_output="x", gate="allow")
    monkeypatch.setenv("TT_JUDGE_LOG_MAX_BYTES", "1")
    lg.record(point="decide", judge="probe", mode="shadow", output="y",
              rule_output="y", gate="allow")
    assert os.path.exists(log_path + ".1")
    assert json.loads(open(log_path, encoding="utf-8").read())["output"] == "y"


def test_evidence는_허용_키만_남긴다(log_path):
    # TV Q4 — 본문·원문 금지, id/code만. 허용 키 밖은 조용히 버린다.
    rec = judge.JudgeLog(log_path).record(
        point="decide", judge="probe", mode="shadow", output="x", rule_output="x",
        gate="allow", evidence=[{"card": "M1"}, {"body": "원문 유출 시도"},
                                {"card": {"nested": True}}])
    ev = rec["evidence"]
    assert ev == [{"card": "M1"}]


def test_쓰기_실패가_기록자를_죽이지_않는다(tmp_path):
    # 리뷰 R5 — 불가능한 경로여도 예외 없이 None(기록 생략)
    blocker = tmp_path / "blocker"
    blocker.write_text("file-as-directory")
    bad = judge.JudgeLog(str(blocker / "log.jsonl"))
    assert bad.record(point="decide", judge="probe", mode="shadow", output="x",
                      rule_output="x", gate="allow") is None
