"""7AMG(M58SPSBY-7AMG): 판단 로그 확장 — evidence·confidence·rule_version·
schema_version, decide action마다 1행(shadow), 보존 정책."""
import json
import os

import pytest

import judge as judge_mod
import probe.core as core
from test_tt_lifecycle import snap, submitted


@pytest.fixture()
def tmp_log(tmp_path):
    return judge_mod.JudgeLog(str(tmp_path / "judge-log.jsonl"))


def read_rows(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def test_log_schema_extended():
    for field in ("evidence", "confidence", "rule_version", "schema_version"):
        assert field in judge_mod.LOG_SCHEMA
    assert judge_mod.SCHEMA_VERSION == 2
    assert judge_mod.CONFIDENCE_LEVELS == ("high", "med", "low")


def test_decide_action_logged_per_row(client, tmp_log):
    """수용기준 2 — 1 사이클 로그 행 수 = decide action 수."""
    issue = submitted(client)  # review 카드 — decide가 tt-review 1건을 낸다
    s = snap(issue)
    acts = core.decide(s)
    assert len(acts) == 1
    for a in acts:
        a["cycle_id"] = None
    judge_mod.log_decide_actions(s, acts, log=tmp_log)
    rows = read_rows(tmp_log.path)
    assert len(rows) == len(acts)
    assert rows[0]["point"] == "decide"
    assert rows[0]["output"] == acts[0]["action"]
    assert rows[0]["card"] == issue["id"]
    assert rows[0]["confidence"] == "high"
    assert rows[0]["rule_version"] == judge_mod.RULE_VERSION
    assert rows[0]["schema_version"] == 2
    assert rows[0]["mode"] == "shadow"


def test_cycle_id_flows_into_rows(client, tmp_log):
    issue = submitted(client)
    s = snap(issue)
    s["cycle_id"] = "cycle-abc"
    acts = core.decide(s)
    judge_mod.log_decide_actions(s, acts, log=tmp_log)
    assert all(r["cycle_id"] == "cycle-abc" for r in read_rows(tmp_log.path))


def test_evidence_is_id_only(client, tmp_log):
    """수용기준 3 — evidence는 id/code만 담는다(키 화이트리스트, 128자 절단)."""
    issue = submitted(client)
    s = snap(issue)
    acts = core.decide(s)
    acts[0]["reason"] = "PR 대상/head/base 변경 — 재제출 필요 " * 30  # 장문도 절단
    acts[0]["body_dump"] = "코멘트 원문을 통째로"  # 허용 키 밖 — 기록되지 않는다
    judge_mod.log_decide_actions(s, acts, log=tmp_log)
    rows = read_rows(tmp_log.path)
    assert rows, "로그 행 없음"
    for r in rows:
        for item in r["evidence"]:
            assert set(item) <= set(judge_mod.EVIDENCE_KEYS)
            for v in item.values():
                assert len(str(v)) <= 128
    assert any(any("reason_code" in item for item in r["evidence"]) for r in rows)
    assert all("body_dump" not in item for r in rows for item in r["evidence"])


def test_evidence_drops_unknown_keys(tmp_log):
    ev = [{"card": "X1", "body": "원문", "sha8": "abcd1234"}]
    assert judge_mod._clean_evidence(ev) == [{"card": "X1", "sha8": "abcd1234"}]


def test_confidence_rejects_non_enum(tmp_log):
    """수용기준 4 — enum 밖 confidence는 거부한다(수치 금지)."""
    with pytest.raises(ValueError):
        tmp_log.record(point="decide", judge="probe", mode="shadow", output="x",
                       rule_output="x", gate="allow", confidence=0.9)
    with pytest.raises(ValueError):
        tmp_log.record(point="decide", judge="probe", mode="shadow", output="x",
                       rule_output="x", gate="allow", confidence="certain")
    # None(구형 스키마 행)은 허용 — 하위 호환
    tmp_log.record(point="p", judge="j", mode="shadow", output="o", rule_output="r",
                   gate="allow", confidence=None)
    assert read_rows(tmp_log.path)[-1]["confidence"] is None


def test_retention_rotates(tmp_log, tmp_path, monkeypatch):
    """수용기준 5 — 상한 초과 시 .1 회전, 본체는 계속 기록 가능."""
    monkeypatch.setenv("TT_JUDGE_LOG_MAX_BYTES", "200")
    for i in range(6):
        tmp_log.record(point="decide", judge="probe", mode="shadow", output=f"a{i}",
                       rule_output="decide", gate="allow", confidence="high",
                       rule_version="r1", schema_version=2)
    assert os.path.exists(tmp_log.path + ".1")
    rotated = read_rows(tmp_log.path + ".1")
    assert len(rotated) >= 1
    rows = read_rows(tmp_log.path)
    assert rows, "회전 후 본체에 최신 행이 있어야 한다"
    assert rows[-1]["output"] == "a5" or len(rotated) + len(rows) == 6
    for r in rotated + rows:
        assert r["schema_version"] == 2


def test_record_failure_does_not_break_decide(tmp_log, tmp_path, monkeypatch):
    """기록 실패(잘못된 경로 포함)가 decide 결과를 바꾸지 않는다 — 동작 불변."""
    broken = judge_mod.JudgeLog(str(tmp_path / "no-dir" / "x" / "log.jsonl"))
    os.makedirs(tmp_path / "no-dir" / "x", exist_ok=True)
    os.chmod(tmp_path / "no-dir" / "x", 0o500)  # 쓰기 불가
    try:
        judge_mod.log_decide_actions({"cycle_id": "c"}, [{"action": "x", "issue": "I"}],
                                     log=broken)  # 예외가 새지 않는다 — 동작 불변 계약
        assert not os.path.exists(broken.path)  # 기록 실패 — 파일이 만들어지지 않는다
    finally:
        os.chmod(tmp_path / "no-dir" / "x", 0o700)


def test_old_style_rows_still_record(tmp_log):
    """구형 호출(확장 필드 없음) — 기존 판단기 개입 행은 그대로 기록된다."""
    tmp_log.record(point="C1", judge="noop", mode="shadow", output="annotate: x",
                   rule_output="unknown", gate="allow", latency_ms=5)
    r = read_rows(tmp_log.path)[-1]
    assert r["point"] == "C1" and r["evidence"] == [] and r["confidence"] is None
