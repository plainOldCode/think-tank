#!/usr/bin/env python3
"""runner 로직 단위 테스트 (stdlib unittest, 네트워크·tmux 불필요 부분까지).

실행: TT_RUNNER_STATE=<tmp> TT_RUNNER_SECRET=<tmpfile> TT_URL=http://127.0.0.1:1 \
     python3 -m unittest discover -s runner -p 'test_*.py' (repo root)
"""
import importlib.util as _ilu
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))

TMP = tempfile.mkdtemp(prefix="tt-runner-test.")
os.environ["TT_RUNNER_STATE"] = os.path.join(TMP, "state")
sec = os.path.join(TMP, "secret")
with open(sec, "w") as f:
    f.write("test-secret-value")
os.environ["TT_RUNNER_SECRET"] = sec
os.environ["TT_URL"] = "http://127.0.0.1:1"
reg = os.path.join(TMP, "agents.json")
with open(reg, "w") as f:
    json.dump({"machine": "testbox", "agents": {
        "p-read": {"driver": "codex", "binary": "/bin/echo", "permission_mode": "read",
                   "workspace": TMP, "allowed_workspaces": [TMP], "timeout_s": 60},
        "p-all": {"driver": "codex", "binary": "/bin/echo", "permission_mode": "all",
                  "workspace": TMP},
        "p-missing": {"driver": "codex", "binary": "/definitely/not/here"},
    }}, f)
os.environ["TT_RUNNER_REGISTRY"] = reg

_spec = _ilu.spec_from_file_location("tt_runner", os.path.join(_HERE, "tt-runner.py"))
R = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(R)


class TestPureLogic(unittest.TestCase):
    def test_run_key(self):
        self.assertEqual(R.run_key({"issue_id": "M1-2", "dispatch_id": 7}), "M1-2#7")

    def test_parse_overrides_allowlist(self):
        opts, body = R.parse_overrides('#opts {"model":"m1","binary":"/evil","continue_session":true}\n실제 메시지\n둘째줄')
        self.assertEqual(opts, {"model": "m1", "continue_session": True})
        self.assertEqual(body, "실제 메시지\n둘째줄")

    def test_parse_overrides_absent(self):
        opts, body = R.parse_overrides("plain message")
        self.assertEqual((opts, body), ({}, "plain message"))

    def test_parse_overrides_bad_json_keeps_message(self):
        opts, body = R.parse_overrides("#opts {broken\nrest")
        self.assertEqual(opts, {})
        self.assertIn("rest", body)

    def test_destructive(self):
        self.assertTrue(R.destructive_hit("please rm -rf /tmp/x"))
        self.assertTrue(R.destructive_hit("git push origin main"))
        self.assertTrue(R.destructive_hit("sudo shutdown now"))
        self.assertIsNone(R.destructive_hit("read the README and summarize"))

    def test_ctx_roundtrip(self):
        tok = R.ctx_token("tt-codex-12")
        self.assertEqual(R.parse_ctx(tok), "tt-codex-12")
        self.assertIsNone(R.parse_ctx("adapter:M1-2"))
        self.assertIsNone(R.parse_ctx(""))

    def test_mask_secret(self):
        self.assertEqual(R.mask("leak test-secret-value here"), "leak *** here")

    def test_workspace_allowlist(self):
        prof = {"workspace": "/Users/YOU/Work", "allowed_workspaces": [TMP]}
        self.assertEqual(R.apply_workspace_override(prof, {"workspace": TMP}), TMP)
        self.assertEqual(R.apply_workspace_override(prof, {"workspace": "/etc"}),
                         "/Users/YOU/Work")
        self.assertEqual(R.apply_workspace_override(prof, {}), "/Users/YOU/Work")

    def test_perm_downgrade_without_tailnet(self):
        orig = R.tailscale_ok
        R.tailscale_ok = lambda: False
        try:
            prof = R.resolve_profile({"agent": "p-all"})
            self.assertEqual(prof["permission_mode"], "auto")
        finally:
            R.tailscale_ok = orig

    def test_missing_binary_rejected(self):
        self.assertIsNone(R.resolve_profile({"agent": "p-missing"}))
        self.assertIsNone(R.resolve_profile({"agent": "nope"}))

    def test_build_argv_codex_no_message_in_argv(self):
        prof = {"driver": "codex", "binary": "codex", "permission_mode": "read",
                "model": "gpt-5-codex", "reasoning": "low"}
        argv = R.build_argv(prof, {}, "/tmp/out.txt")
        self.assertIn("exec", argv)
        self.assertIn("-", argv)
        self.assertIn("read-only", argv)
        self.assertIn("gpt-5-codex", argv)
        self.assertIn("model_reasoning_effort=low", argv)
        joined = " ".join(argv)
        # message 본문은 절대 argv에 못 들어간다: prompt는 '-'(stdin 파일)
        self.assertEqual(argv[argv.index("exec") + 1], "-")
        self.assertNotIn("실제 작업 요청 본문", joined)


class TestWatchUpgrade(unittest.TestCase):
    """M3BZS1G3-VNQH 4범위: BLOCKED 판정 / stall / workspace 불변식 / credential 격리."""

    # ① 입력필요 vs 크래시
    def test_input_needed_hits(self):
        for s in ("Error: this action requires approval",
                  "Approval required before proceeding",
                  "Waiting for your input...",
                  "Overwrite? [y/N]",
                  "needs_input: tool external_permission"):
            self.assertIsNotNone(R.input_needed(s), s)

    def test_input_needed_no_false_positive(self):
        # 성공 출력에서 단어가 우연히 등장 — finalize는 exit!=0에만 calling, 문구 자체도 non-hit
        for s in ("All tests passed, no approvals were needed for this run",
                  "The report has 3 input fields and 2 output fields",
                  "run finished successfully"):
            self.assertIsNone(R.input_needed(s), s)

    def test_finalize_blocked_vs_crash(self):
        R.save_runs({"T-3#201": {"status": "running", "issue_id": "T-3",
                                 "dispatch_id": 201, "session": "tt-x-201"}})
        base = os.path.join(R.RUNTIME_DIR, "T-3_201.log")
        with open(base, "w") as f:
            f.write("ERROR: Sandbox: user approval required for network access\n")
        R.tt_comment = lambda issue, body: None  # 네트워크 차단
        outcome = R.finalize(None, "T-3#201", R.get_run("T-3#201"), 1)
        self.assertEqual(outcome, "blocked")
        self.assertEqual(R.get_run("T-3#201")["status"], "blocked")
        self.assertIn("approval", R.get_run("T-3#201")["blocked_on"])
        R.save_runs({"T-3#202": {"status": "running", "issue_id": "T-3",
                                 "dispatch_id": 202, "session": "tt-x-202"}})
        with open(os.path.join(R.RUNTIME_DIR, "T-3_202.log"), "w") as f:
            f.write("segmentation fault (core dumped)\n")
        outcome = R.finalize(None, "T-3#202", R.get_run("T-3#202"), 1)
        self.assertEqual(outcome, "failed")

    # ② stall
    def test_stall_fingerprint_and_kill_thresholds(self):
        R.save_runs({"T-4#301": {"status": "running", "issue_id": "T-4",
                                 "dispatch_id": 301, "session": "tt-x-301"}})
        now = time.time()
        killed = []
        R.tmux_run = lambda *a, **k: killed.append(a) or subprocess.CompletedProcess(a, 0)
        orig_cap = R.pane_fingerprint
        R.pane_fingerprint = lambda name, key=None: "fixedfp"
        try:
            # 첫 관측: baseline 기록
            self.assertFalse(R.stall_check("T-4#301", R.get_run("T-4#301"), now))
            ent = R.get_run("T-4#301")
            self.assertEqual(ent["pane_fp"], "fixedfp")
            # 임계 내 무음: 아무 일 없음
            self.assertFalse(R.stall_check("T-4#301", ent, now + R.STALL_SILENCE_S - 30))
            # 임계 초과: STALL 코멘트 1회
            self.assertTrue(R.stall_check("T-4#301", R.get_run("T-4#301"), now + R.STALL_SILENCE_S + 5))
            self.assertTrue(R.get_run("T-4#301")["stall_notified"])
            self.assertFalse(R.stall_check("T-4#301", R.get_run("T-4#301"), now + R.STALL_SILENCE_S + 10))
            # 재지문 변경(출력 재개) → 카운터 리셋
            R.pane_fingerprint = lambda name, key=None: "newfp"
            self.assertFalse(R.stall_check("T-4#301", R.get_run("T-4#301"), now + R.STALL_SILENCE_S + 20))
            # kill 임계 초과 → kill + stall-killed 장부
            R.pane_fingerprint = lambda name, key=None: "newfp"
            ent = R.get_run("T-4#301")
            self.assertTrue(R.stall_check("T-4#301",
                                          dict(ent, pane_ts=now),
                                          now + R.STALL_SILENCE_S + R.STALL_KILL_AFTER_S + 5))
            self.assertEqual(R.get_run("T-4#301")["detail"], "stall-killed")
        finally:
            R.pane_fingerprint = orig_cap

    # ③ 워크스페이스 불변식
    def test_guard_workspace_roots(self):
        prof = {"workspace": TMP, "allowed_workspaces": [TMP]}
        real, why = R.guard_workspace(prof, os.path.join(TMP, "sub/../"))
        self.assertIsNone(why)
        self.assertEqual(real, os.path.realpath(TMP))
        real, why = R.guard_workspace(prof, "/etc")
        self.assertIsNone(real)
        self.assertEqual(why, "outside-root")
        real, why = R.guard_workspace(prof, "/tmp/does-not-exist-xyz")
        self.assertEqual(why, "not-a-dir")

    def test_sanitize_workspace_value(self):
        self.assertIsNone(R.sanitize_workspace_value("evil\n/path"))
        self.assertIsNone(R.sanitize_workspace_value("/path\x00"))
        self.assertIsNone(R.sanitize_workspace_value(""))
        self.assertIsNone(R.sanitize_workspace_value("x" * 600))
        self.assertEqual(R.sanitize_workspace_value("/Users/YOU/Work"), "/Users/YOU/Work")

    def test_override_sanitize_falls_back(self):
        prof = {"workspace": TMP, "allowed_workspaces": [TMP]}
        self.assertEqual(R.apply_workspace_override(prof, {"workspace": TMP + "\nhack"}), TMP)

    # ④ credential 격리
    def test_agent_env_drops_secrets(self):
        os.environ["MY_APP_TOKEN"] = "tok"
        os.environ["DATABASE_PASSWORD"] = "pw"
        os.environ["TT_RUNNER_STATE_EXTRA"] = "x"
        os.environ["HARMLESS_SETTING"] = "keep-me"
        env = R.agent_env({"env_extra": {"CODEX_HOME": "/tmp/ch"}})
        for k in ("MY_APP_TOKEN", "DATABASE_PASSWORD", "TT_RUNNER_STATE_EXTRA",
                  "TT_RUNNER_SECRET", "TT_SECRET"):
            self.assertNotIn(k, env)
        self.assertEqual(env["HARMLESS_SETTING"], "keep-me")
        self.assertEqual(env["CODEX_HOME"], "/tmp/ch")  # registry opt-in만 통과
        # 값에 secret을 품은 키도 낙찰 (secret=test-secret-value)
        os.environ["ODD_NAME"] = "prefix-test-secret-value-suffix"
        self.assertNotIn("ODD_NAME", R.agent_env({}))


class TestLedgerGate(unittest.TestCase):
    P = {"dispatch_id": 101, "issue_id": "T-1", "agent": "p-read",
         "message": "hello", "context": ""}

    def test_check_and_set_single_entry(self):
        s1, fresh1 = R.prepare(self.P)
        self.assertTrue(fresh1)
        s2, fresh2 = R.prepare(self.P)  # polling 중복 시뮬레이션
        self.assertFalse(fresh2)
        self.assertEqual(s2, s1)

    def test_ledger_fields(self):
        R.prepare({"dispatch_id": 102, "issue_id": "T-1", "agent": "p-read",
                   "message": '#opts {"model":"x"}\nbody here', "context": ""})
        ent = R.get_run("T-1#102")
        self.assertEqual(ent["message"], "body here")
        self.assertEqual(ent["opts"], {"model": "x"})
        self.assertEqual(ent["status"], "queued")

    def test_contract_reaches_new_and_resumed_agent_prompts(self):
        from unittest.mock import patch
        contract = {"version": "tt-tdd-v1:test", "instructions": "TDD: RED 후 GREEN. 대체 검증은 사유 기록."}
        p = {"dispatch_id": 104, "issue_id": "CONTRACT-1", "agent": "p-read",
             "message": '#opts {"model":"chosen"}\nFix retry', "context": "",
             "work_contract": contract, "execution_attempt": 2}
        session, fresh = R.prepare(p)
        self.assertTrue(fresh)
        key = R.run_key(p)
        ent = R.get_run(key)
        self.assertEqual(ent["message"], "Fix retry")
        self.assertEqual(ent["opts"], {"model": "chosen"})
        with patch.object(R.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)):
            msgfile = R.start_session(key, {}, session, TMP)
        with open(msgfile) as f:
            prompt = f.read()
        self.assertIn(contract["instructions"], prompt)
        self.assertIn(contract["version"], prompt)
        self.assertIn("CONTRACT-1", prompt)
        self.assertIn("execution_attempt=2", prompt)
        self.assertTrue(prompt.endswith("Fix retry"))
        with patch.object(R.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)):
            self.assertTrue(R.continue_session({"continue_cmd": "echo"}, session, "Fix retry", key))
        with open(os.path.join(R.INBOX_DIR, R.sanitize(session) + ".msg")) as f:
            self.assertEqual(f.read(), prompt)

    def test_legacy_payload_keeps_its_prompt(self):
        from unittest.mock import patch
        p = {"dispatch_id": 105, "issue_id": "LEGACY-1", "agent": "p-read", "message": "legacy task"}
        session, _ = R.prepare(p)
        with patch.object(R.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)):
            msgfile = R.start_session(R.run_key(p), {}, session, TMP)
        with open(msgfile) as f:
            self.assertEqual(f.read(), "legacy task")

    def test_held_lookup_and_release(self):
        R.prepare({"dispatch_id": 103, "issue_id": "T-2", "agent": "p-read",
                   "message": "rm -rf stuff", "context": ""})
        R.update_run("T-2#103", status="held", gate="rm")
        self.assertEqual(R.latest_held("T-2"), "T-2#103")
        self.assertIsNone(R.latest_held("T-9"))

    def test_sanitize_session_name(self):
        name = R.new_session_name({"agent": "codex m2max/한글", "dispatch_id": 9})
        self.assertTrue(name.startswith("tt-codex-m2max--"))
        self.assertNotIn("한글", name)


class TestReconcileRelease(unittest.TestCase):
    """서버 reconcile 명령 처리 (M3BZS1FS-5722 ③): 장부 cancelled + kill.
    장부는 모듈 전역이라 테스트별 issue id를 분리한다."""

    def setUp(self):
        self.n = str(self.id()).rsplit(".", 1)[-1]  # 테스트명 접미로 key 분리
        self.issue = "REL-" + self.n
        self.key = self.issue + "#201"
        self.p = {"dispatch_id": 201, "issue_id": self.issue, "agent": "p-read",
                  "message": "긴 작업", "context": ""}
        R.prepare(self.p)

    def test_release_cancels_active_run(self):
        R.update_run(self.key, status="running", session="tt-rel-" + self.n)
        killed = R.release_issue(self.issue, "카드 done 전이")
        self.assertEqual(killed, ["tt-rel-" + self.n])  # 세션 없으면 kill 실패해도 목록에 남음
        ent = R.get_run(self.key)
        self.assertEqual(ent["status"], "cancelled")
        self.assertEqual(ent["detail"], "release-command")

    def test_release_blocks_later_execution(self):
        # cancelled된 장부는 execute_once의 queued 게이트에서 영구 차단
        R.update_run(self.key, status="running", session="tt-rel-" + self.n)
        R.release_issue(self.issue, "cancelled 전이")
        R.execute_once(self.p, "tt-rel-" + self.n)  # 시작 시도해도 status는 cancelled 유지
        self.assertEqual(R.get_run(self.key)["status"], "cancelled")

    def test_release_noop_for_other_issue(self):
        self.assertEqual(R.release_issue("NO-SUCH-9"), [])
        self.assertEqual(R.get_run(self.key)["status"], "queued")

    def test_release_held_run(self):
        R.update_run(self.key, status="held", gate="rm")
        killed = R.release_issue(self.issue)  # held도 중지 대상 (queued 게이트 우회 승인 방지)
        self.assertEqual(len(killed), 1)  # prepare가 붙인 세션명으로 kill 시도
        self.assertEqual(R.get_run(self.key)["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
