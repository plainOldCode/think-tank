"""G1DD(M58SPSRK-G1DD): 스키마/규칙 버전 정본 + 판단 로그 상속 + fail-to-rules."""
import json

import failure_rules
import judge as judge_mod
import ontology
from ontology.schema import graph_compatible


def test_judge_versions_inherit_canonical_constants():
    """수용기준 3 — 판단 로그의 rule_version/schema_version은 정본 상수와 같다."""
    assert judge_mod.SCHEMA_VERSION == ontology.SCHEMA_VERSION
    assert judge_mod.RULE_VERSION == failure_rules.RULE_VERSION


def test_log_rows_carry_canonical_versions(tmp_path):
    lg = judge_mod.JudgeLog(str(tmp_path / "jl.jsonl"))
    lg.record(point="decide", judge="probe", mode="shadow", output="x",
              rule_output="x", gate="allow", confidence="high",
              rule_version=failure_rules.RULE_VERSION,
              schema_version=ontology.SCHEMA_VERSION)
    rows = [json.loads(l) for l in open(lg.path, encoding="utf-8") if l.strip()]
    assert rows[-1]["rule_version"] == failure_rules.RULE_VERSION
    assert rows[-1]["schema_version"] == ontology.SCHEMA_VERSION


def test_graph_version_mismatch_rejects():
    """수용기준 2 — 버전 불일치 그래프는 소비 금지(fail-to-rules)."""
    assert graph_compatible(ontology.SCHEMA_VERSION) is True
    assert graph_compatible(ontology.SCHEMA_VERSION - 1) is False
    assert graph_compatible(ontology.SCHEMA_VERSION + 1) is False
    assert graph_compatible("2") is False  # 문자열 버전도 불일치


def test_rule_version_is_stable_string():
    assert isinstance(failure_rules.RULE_VERSION, str) and failure_rules.RULE_VERSION


def test_changelog_documents_version_convention():
    text = open("CHANGELOG.md", encoding="utf-8").read()
    assert "## 버전 규약" in text
    assert "SCHEMA_VERSION" in text and "RULE_VERSION" in text
    assert "graph_compatible" in text and "fail-to-rules" in text
