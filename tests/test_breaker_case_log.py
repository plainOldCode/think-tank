"""XVY7: 타이브레이커 케이스 로그 검증 스크립트 (scripts/tt-case-log-check.py)."""
import json
import pathlib
import subprocess
import sys

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "tt-case-log-check.py"

SEED_RTZ1 = {"trigger_seq": 1640, "card": "M40MP7EX-RTZ1", "reason_code": "unknown",
             "detector": "probe", "decider": "supervisor-agent",
             "decision": "review-fix가 실행 불가 어댑터로 재발령돼 정체 — 수령·수정.",
             "followup_card": None}
SEED_XF9W = {"trigger_seq": 1624, "card": "M4CNE298-XF9W", "reason_code": "no_pr",
             "detector": "probe", "decider": "supervisor-agent",
             "decision": "PR 없는 보고 접수 → needs-merge a1, 병합 실측 후 verify."}


def run(tmp_path, records, raw=None):
    p = tmp_path / "breakers.jsonl"
    body = raw if raw is not None else "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n"
    p.write_text(body, encoding="utf-8")
    return subprocess.run([sys.executable, str(SCRIPT), str(p)],
                          capture_output=True, text=True, timeout=30)


def test_seed_records_pass(tmp_path):
    r = run(tmp_path, [SEED_RTZ1, SEED_XF9W])
    assert r.returncode == 0, r.stderr
    assert "OK — 2건" in r.stdout


def test_missing_field_fails(tmp_path):
    bad = {k: v for k, v in SEED_RTZ1.items() if k != "decider"}
    r = run(tmp_path, [bad])
    assert r.returncode == 1
    assert "필수 필드 누락 'decider'" in r.stderr


def test_unknown_reason_code_fails(tmp_path):
    bad = dict(SEED_RTZ1, reason_code="made_up_code")
    r = run(tmp_path, [bad])
    assert r.returncode == 1
    assert "없음" in r.stderr


def test_duplicate_rejected(tmp_path):
    r = run(tmp_path, [SEED_RTZ1, dict(SEED_RTZ1)])
    assert r.returncode == 1
    assert "중복" in r.stderr


def test_bad_detector_fails(tmp_path):
    bad = dict(SEED_XF9W, detector="robot")
    r = run(tmp_path, [bad])
    assert r.returncode == 1
    assert "detector" in r.stderr


def test_bad_json_line_exit_1(tmp_path):
    r = run(tmp_path, None, raw="{not json}\n")
    assert r.returncode == 1


def test_missing_file_exit_2(tmp_path):
    r = subprocess.run([sys.executable, str(SCRIPT), str(tmp_path / "none.jsonl")],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 2
