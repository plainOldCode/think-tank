"""GitHub Actions CI 워크플로 구조 회귀 (TT M3PPRJ49-FYW2 CI 카드, 표준 라이브러리만).

의도: probe(순회 감시자)가 필수 검사 이름으로 판정하는集合과 워크플로 job id가
정확히 일치해야 한다. 어긋나면 probe는 존재하지 않는 검사나 다른 이름의 검사에
green 판정을 내린다 — 이 테스트가 그 어긋남을 PR 단계에서 실패시킨다.
누락·실패한 검사가 green으로 오인되지 않게 하려면 job 이름 고정과
exit code 삼킴(|| true, continue-on-error) 금지가 본 테스트의 실동작 단속 대상이다.

실행: python3 -m unittest tests.test_ci_workflow  또는  pytest tests/test_ci_workflow.py
job id 목록은 probe 카드의 REQUIRED_CHECKS와 동기 유지(현재: pytest, smoke-node, secret-scan).
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WF = os.path.join(ROOT, ".github", "workflows", "ci.yml")

REQUIRED_CHECKS = ["pytest", "smoke-node", "secret-scan"]


def wf_text():
    with open(WF, encoding="utf-8") as f:
        return f.read()


def job_ids(text):
    m = re.search(r"(?m)^jobs:", text)
    assert m, "jobs: 최상위 키 없음"
    body = text[m.end():]
    return re.findall(r"(?m)^  ([A-Za-z0-9_-]+):\s*$", body)


class CiWorkflow(unittest.TestCase):
    def test_workflow_exists(self):
        self.assertTrue(os.path.isfile(WF), ".github/workflows/ci.yml 가 존재해야 한다")

    def test_job_ids_are_exactly_required_checks(self):
        ids = set(job_ids(wf_text()))
        self.assertEqual(ids, set(REQUIRED_CHECKS),
                         "probe REQUIRED_CHECKS와 job id 집합이 달라지면 green 판정 근거가 무너진다")

    def test_triggers(self):
        text = wf_text()
        m = re.search(r"(?m)^on:", text)
        self.assertIsNotNone(m, "on: 블록 없음")
        block = text[m.end():m.end() + 400] if m else ""
        self.assertIn("pull_request:", block)
        for ev in ("opened", "synchronize", "reopened"):
            self.assertIn(ev, block, f"pull_request 이벤트 {ev} 누락")
        self.assertIn("push:", block)
        self.assertIn("main", block, "push 브랜치에 main 포함(커버리지 회귀용)")

    def test_permissions_read_only(self):
        text = wf_text()
        m = re.search(r"(?m)^permissions:\s*$", text)
        self.assertIsNotNone(m, "최상위 permissions: 블록 없음")
        block = text[m.end():] if m else ""
        m2 = re.search(r"(?m)^[A-Za-z]", block)
        top = block[:m2.start()] if m2 else block
        self.assertIn("contents: read", top)
        self.assertIsNone(re.search(r"(?m)^\s+(write|admin)\b", top), " 쓰기/관리 권한 금지")

    def test_no_secrets_or_output_leakage(self):
        text = wf_text()
        self.assertNotIn("secrets.", text, "워크플로는 시크릿을 참조하지 않는다(외부 PR 노출 차단)")
        self.assertNotIn("${{", text.split("jobs:")[1] if "jobs:" in text else "",
                         "jobs 본문에 expression 없음(주입 표면 최소화)")

    def test_no_exit_code_swallowing(self):
        text = wf_text()
        self.assertNotIn("|| true", text)
        self.assertNotIn("continue-on-error", text)

    def test_pytest_job_uses_requirements(self):
        text = wf_text()
        self.assertIn("pip install -r requirements.txt", text)
        self.assertIn("pytest -q", text)

    def test_smoke_job_runs_all_three_node_smokes(self):
        text = wf_text()
        for f in ("tests/smoke_board_legacy_quiet.mjs",
                  "tests/smoke_board_progress.mjs",
                  "tests/smoke_mobile.mjs"):
            self.assertIn("node " + f, text, f"{f} 실행 라인 누락")

    def test_secret_scan_job(self):
        self.assertIn("bash scripts/secret-scan.sh", wf_text())

    def test_concurrency_group(self):
        text = wf_text()
        m = re.search(r"(?m)^concurrency:", text)
        self.assertIsNotNone(m, "concurrency 블록 없음(중복 실행 방지)")
        self.assertIn("cancel-in-progress: true", text)


class Requirements(unittest.TestCase):
    def test_requirements_txt_covers_imports(self):
        path = os.path.join(ROOT, "requirements.txt")
        self.assertTrue(os.path.isfile(path), "requirements.txt 존재")
        pkgs = {re.split(r"[<>=!;\[ ]", ln.strip())[0].lower()
                for ln in open(path, encoding="utf-8")
                if ln.strip() and not ln.strip().startswith("#")}
        # server/tests 실측 import: fastapi, pydantic(stdlib sqlite3 제외), uvicorn(test_contract_cli),
        # TestClient(pytest 9 기준 httpx 필수), pytest
        for p in ("fastapi", "pydantic", "uvicorn", "pytest", "httpx"):
            self.assertIn(p, pkgs, f"requirements.txt에 {p} 필요")


if __name__ == "__main__":
    unittest.main()
