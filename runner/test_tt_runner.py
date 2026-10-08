#!/usr/bin/env python3
"""runner 로직 단위 테스트 (stdlib unittest, 네트워크·tmux 불필요 부분까지).

실행: TT_RUNNER_STATE=<tmp> TT_RUNNER_SECRET=<tmpfile> TT_URL=http://127.0.0.1:1 \
     python3 -m unittest discover -s runner -p 'test_*.py' (repo root)
"""
import glob
import http.client
import importlib.util as _ilu
import json
import os
import subprocess
import sys
import tempfile
import threading
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
    # ② stall — 상태파일 heartbeat 프로토콜 (TT 개선#1 요구 4)
    def _hb(self, key, fp, pane_ts, round_no=1, session="tt-x-301"):
        R.write_heartbeat(key, {"ts": time.time(), "round": round_no,
                                "dispatch_id": 301, "session": session,
                                "pane_fp": fp, "pane_ts": pane_ts})

    def test_stall_heartbeat_thresholds(self):
        R.save_runs({"T-4#301": {"status": "running", "issue_id": "T-4",
                                 "dispatch_id": 301, "session": "tt-x-301", "round": 1}})
        now = time.time()
        killed = []
        R.tmux_run = lambda *a, **k: killed.append(a) or subprocess.CompletedProcess(a, 0)
        # heartbeat 부재 → 판정 스킵(보수적)
        self.assertFalse(R.stall_check("T-4#301", R.get_run("T-4#301"), now))
        # 임계 내 무음: 아무 일 없음
        self._hb("T-4#301", "fixedfp", now - 30)
        self.assertFalse(R.stall_check("T-4#301", R.get_run("T-4#301"), now))
        # 임계 초과: STALL 코멘트 1회 + 장부 stalled(회차 가드)
        self._hb("T-4#301", "fixedfp", now - R.STALL_SILENCE_S - 5)
        self.assertTrue(R.stall_check("T-4#301", R.get_run("T-4#301"), now))
        self.assertEqual(R.get_run("T-4#301")["status"], "stalled")
        # 같은 스냅샷 재판정 — stall_notified 세팅으로 재통지 없음
        self._hb("T-4#301", "fixedfp", now - R.STALL_SILENCE_S - 10)
        self.assertFalse(R.stall_check("T-4#301", R.get_run("T-4#301"), now))
        # kill 임계 초과 → kill + stall-killed 장부
        self._hb("T-4#301", "fixedfp", now - R.STALL_SILENCE_S - R.STALL_KILL_AFTER_S - 5)
        self.assertTrue(R.stall_check("T-4#301", R.get_run("T-4#301"), now))
        self.assertEqual(R.get_run("T-4#301")["detail"], "stall-killed")

    def test_stall_heartbeat_round_mismatch_skips(self):
        """stale 감시자 방어 — 파일 round ≠ 스냅샷 round면 판정 자체를 스킵한다.

        이전 회차 감시자가 현재 회차 세션을 STALL-kill하는 부류의 구조적 차단."""
        now = time.time()
        R.save_runs({"T-4#301": {"status": "running", "issue_id": "T-4",
                                 "dispatch_id": 301, "session": "tt-x-301", "round": 1}})
        stale_snap = R.get_run("T-4#301")
        # 재회차 — 장부가 round 2로 교체, heartbeat도 현재 회차(2) 기준
        R.save_runs({"T-4#301": {"status": "running", "issue_id": "T-4",
                                 "dispatch_id": 301, "session": "tt-x-301-r2", "round": 2}})
        self._hb("T-4#301", "fixedfp", now - R.STALL_SILENCE_S * 3, round_no=2)
        # round 1 스냅샷의 늦은 판정 → 스킵 (킬도 코멘트도 없음)
        self.assertFalse(R.stall_check("T-4#301", stale_snap, now))
        self.assertEqual(R.get_run("T-4#301")["status"], "running")

    def test_stall_kill_write_round_guarded(self):
        """킬 직전 재회차가 끼어들면 장부 쓰기는 거부된다(세션 킬은 물리 행위)."""
        now = time.time()
        R.save_runs({"T-4#301": {"status": "running", "issue_id": "T-4",
                                 "dispatch_id": 301, "session": "tt-x-301", "round": 1}})
        snap = R.get_run("T-4#301")
        self._hb("T-4#301", "fixedfp", now - R.STALL_SILENCE_S - R.STALL_KILL_AFTER_S - 5)
        # 판정과 쓰기 사이 재회차 발생 — update_run_if_round가 거부
        R.save_runs({"T-4#301": {"status": "queued", "issue_id": "T-4",
                                 "dispatch_id": 301, "session": "tt-x-301-r2", "round": 2}})
        self.assertTrue(R.stall_check("T-4#301", snap, now))
        self.assertEqual(R.get_run("T-4#301")["status"], "queued")  # 훼손 없음

    def test_heartbeat_pass_captures_and_accumulates_silence(self):
        R.save_runs({"T-4#301": {"status": "running", "issue_id": "T-4",
                                 "dispatch_id": 301, "session": "tt-x-301", "round": 1}})
        orig_fp = R.pane_fingerprint
        try:
            R.pane_fingerprint = lambda name, key=None: "fp1"
            R.heartbeat_pass()
            hb1 = R.read_heartbeat("T-4#301")
            self.assertEqual(hb1["pane_fp"], "fp1")
            self.assertEqual(hb1["round"], 1)
            # 지문 불변 — pane_ts 유지(무음 누적)
            R.heartbeat_pass()
            hb2 = R.read_heartbeat("T-4#301")
            self.assertEqual(hb2["pane_ts"], hb1["pane_ts"])
            # 지문 변경 — pane_ts 갱신 + stalled 복구
            R.update_run("T-4#301", status="stalled", stall_notified=True)
            R.pane_fingerprint = lambda name, key=None: "fp2"
            R.heartbeat_pass()
            hb3 = R.read_heartbeat("T-4#301")
            self.assertEqual(hb3["pane_fp"], "fp2")
            self.assertGreater(hb3["pane_ts"], hb2["pane_ts"])
            self.assertEqual(R.get_run("T-4#301")["status"], "running")
        finally:
            R.pane_fingerprint = orig_fp


    def test_heartbeat_round_change_resets_silence_baseline(self):
        """리뷰 R7 — 지문은 회차 ID가 아니다: 회차 교체 후 동일 지문이어도 이전
        회차 무음을 승계하면 안 된다(새 회차 = 새 기준)."""
        R.save_runs({"T-4#302": {"status": "running", "issue_id": "T-4",
                                 "dispatch_id": 302, "session": "tt-x-302", "round": 1}})
        orig_fp = R.pane_fingerprint
        try:
            R.pane_fingerprint = lambda name, key=None: "fp-same"
            R.heartbeat_pass()
            hb1 = R.read_heartbeat("T-4#302")
            self.assertEqual(hb1["round"], 1)
            # 회차 교체(새 엔트리 기록 — 러너 재회차 경로와 동일한 모양)
            ent = R.get_run("T-4#302")
            ent.update({"round": 2, "session": "tt-x-302-fp-same-r2"})
            R.save_runs({"T-4#302": ent})
            R.heartbeat_pass()
            hb2 = R.read_heartbeat("T-4#302")
            self.assertEqual(hb2["round"], 2)
            # 지문이 같아도 회차가 바뀌었으므로 pane_ts는 새로 시작
            self.assertGreater(hb2["pane_ts"], hb1["pane_ts"])
        finally:
            R.pane_fingerprint = orig_fp

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


class TestProgressProjection(unittest.TestCase):
    """TT M3EREF97-FXWQ: 진행 투영은 tt_progress 한 지점만 통과하고 코멘트를 건드리지 않는다."""

    def setUp(self):
        self.calls = []
        self.comments = []
        self._orig_progress = R.tt_progress
        self._orig_comment = R.tt_comment
        # 이전 클래스가 남긴 pending 보고 정리(공유 RUNTIME_DIR — 호출 순서 단정 보호)
        for f in glob.glob(os.path.join(R.RUNTIME_DIR, "*.pending.json")):
            os.remove(f)

        def _prog(iid, did, payload):
            self.calls.append((str(iid), did, payload))
            return True  # 서버 200 — 통합 접수 성공 (코멘트는 payload에 실려간다)

        R.tt_progress = _prog
        R.tt_comment = lambda iid, body: self.comments.append((str(iid), body))
        R.save_runs({"P-1#501": {"status": "running", "issue_id": "P-1",
                                 "dispatch_id": 501, "session": "tt-p-501",
                                 "started": time.time() - 100}})

    def tearDown(self):
        R.tt_progress = self._orig_progress
        R.tt_comment = self._orig_comment

    def _states(self):
        return [c[2]["state"] for c in self.calls]

    def test_finalize_projects_terminal_states(self):
        ent = {"issue_id": "P-1", "dispatch_id": 501, "session": "tt-p-501"}
        R.finalize(None, "P-1#501", ent, 0)
        self.assertEqual(self._states(), ["finished"])
        self.calls.clear()
        # 입력 요구 시그니처 BLOCKED: 세션 종료 → failed 투영 (코멘트는 현행 그대로)
        # (보고 소유권 게이트 — 회차당 보고 1회이므로 판정은 별도 런으로, PR#47 R3)
        R.save_runs({"P-5#505": {"status": "running", "issue_id": "P-5",
                                 "dispatch_id": 505, "session": "tt-p-505"}})
        with open(os.path.join(R.RUNTIME_DIR, "P-5_505.log"), "w") as f:
            f.write("ERROR: approval required before proceeding\n")
        R.finalize(None, "P-5#505", R.get_run("P-5#505"), 1)
        self.assertEqual(self._states(), ["failed"])
        # 완료 보고는 progress payload에 통합 접수된다(R3) — 별도 /comments 없음
        p5 = [c for c in self.calls if c[0] == "P-5"][-1]
        self.assertIn("BLOCKED", p5[2].get("comment", ""))

    def test_stall_check_projects_stalled_once_and_kill_failed(self):
        ent = R.get_run("P-1#301") if R.get_run("P-1#301") else None
        R.save_runs({"P-2#502": {"status": "running", "issue_id": "P-2",
                                 "dispatch_id": 502, "session": "tt-p-502"}})
        now = time.time()
        R.tmux_run = lambda *a, **k: subprocess.CompletedProcess(a, 0)
        orig_cap = R.pane_fingerprint
        R.pane_fingerprint = lambda name, key=None: "fp-fixed"
        try:
            def hb(pane_ts):
                R.write_heartbeat("P-2#502", {"ts": now, "round": None, "dispatch_id": 502,
                                              "session": "tt-p-502", "pane_fp": "fp-fixed",
                                              "pane_ts": pane_ts})
            e2 = R.get_run("P-2#502")
            hb(now)  # 관찰 baseline(pane_ts=now) — 이후 판정 시각만 앞선다
            R.stall_check("P-2#502", e2, now)
            R.stall_check("P-2#502", R.get_run("P-2#502"), now + R.STALL_SILENCE_S + 5)
            self.assertEqual(self._states(), ["stalled"])  # STALL 코멘트와 1:1, 중복 갱신 없음
            self.calls.clear()
            R.stall_check("P-2#502", R.get_run("P-2#502"), now + R.STALL_SILENCE_S + 10)
            self.assertEqual(self._states(), [])
            R.stall_check("P-2#502", R.get_run("P-2#502"),
                          now + R.STALL_SILENCE_S + R.STALL_KILL_AFTER_S + 5)
            self.assertEqual(self._states(), ["failed"])  # STALL-kill → failed
            # 투영은 별도 경로: STALL 코멘트는 존재하지만 진행 코멘트는 없다
            self.assertTrue(any("STALL" in b for _, b in self.comments))
        finally:
            R.pane_fingerprint = orig_cap

    def test_release_projects_finished_without_progress_comments(self):
        R.save_runs({"P-3#503": {"status": "running", "issue_id": "P-3",
                                 "dispatch_id": 503, "session": ""}})
        R.release_issue("P-3", "카드 terminalize")
        self.assertEqual(self._states(), ["finished"])
        self.assertEqual(len(self.comments), 1)  # 기존 cancelled 코멘트 1건뿐

    def test_watch_tick_running_then_stalled_no_flipback(self):
        ent = {"status": "running", "issue_id": "P-4", "dispatch_id": 504,
               "session": "tt-p-504", "started": time.time() - 60}
        R.save_runs({"P-4#504": ent})
        fp = ["a", "a"]  # 첫 tick은 진행, 이후 무음 고정
        state = {"i": 0}
        orig_cap, orig_tail = R.pane_fingerprint, R.session_tail
        orig_alive = R.session_alive
        R.session_alive = lambda name: True
        R.tmux_run = lambda *a, **k: subprocess.CompletedProcess(a, 0)
        def cap(name, key=None):
            i = state["i"]; state["i"] += 1
            return fp[min(i, 1)]
        R.pane_fingerprint = cap
        R.session_tail = lambda name, limit=200: "step 1\nstep 2"
        try:
            R.watch_once()  # tick 1: 지문 변경 → running + tail 마지막 줄
            first = self.calls[0]
            self.assertEqual(first[2]["state"], "running")
            self.assertEqual(first[2]["tail"], "step 2")
            self.assertTrue(self.comments == [])  # 진행은 코멘트 0
            # stall 임계 초과 무음: 진행 tick은 스킵 — running 재투영이 없어야 한다
            # (stalled 투영 자체는 stall_check의 STALL 코멘트 지점이 소유: 1st 순회에 1건)
            self.calls.clear()
            past = time.time()
            R.update_run("P-4#504", pane_ts=past)  # baseline 지문 시각 리셋(킬 임계 방지)
            R.update_run("P-4#504", pane_fp="a")
            orig_stall = R.STALL_SILENCE_S
            try:
                R.STALL_SILENCE_S = 0  # 이번 tick부터 stall 판정 유효하게
                R.watch_once()
                self.assertNotIn("running", self._states())
                R.update_run("P-4#504", pane_ts=time.time())
                R.watch_once()
                self.assertNotIn("running", self._states())
            finally:
                R.STALL_SILENCE_S = orig_stall
        finally:
            R.pane_fingerprint = orig_cap
            R.session_tail = orig_tail


class TestDupSkipReRound(unittest.TestCase):
    """think-tank#46: 동일 (issue, dispatch_id) 재전송 + message의 대상 sha 변경 → 재실행.

    기존 계약: 장부 키 "<issue>#<dispatch>" 단일 실행 게이트. 결함: status=done이면
    대상 sha가 바뀌어도 영구 DUP-SKIP → 재검토 체인 정지. 수정(제안 A): done이어도
    대상 sha가 다르면 새 회차로 재실행. 스킵 시 대상 불일치 여부를 명시(제안 C).
    """

    DID = 401

    def setUp(self):
        self.n = str(self.id()).rsplit(".", 1)[-1]
        self.issue = "DUPR-" + self.n
        # 실측 형식(장부 M45WT02H-CWES#145): 대상 sha는 첫 줄, 본문에 다른 sha도 등장
        self.msg_a = ("[auto review] PR #3 (plainOldCode/services-page) @ 85d6670a — "
                      "카드 %s 리뷰 요청(재검토: 브랜치가 main 7aac56d 위로 rebase됨, "
                      "이전 기준 SHA f177b4f 판정 무효). 먼저 claim-review를 호출하라." % self.issue)
        self.msg_b = ("[auto review] PR #3 (plainOldCode/services-page) @ 07bcc393 — "
                      "카드 %s 리뷰 요청(재검토: codex 판정 SEC-1 수정 반영 커밋)." % self.issue)
        self.p1 = {"dispatch_id": self.DID, "issue_id": self.issue, "agent": "p-read",
                   "message": self.msg_a, "context": ""}
        self.key = "%s#%d" % (self.issue, self.DID)
        R.prepare(self.p1)
        self.orig_comment = R.tt_comment

    def tearDown(self):
        R.tt_comment = self.orig_comment

    def _done(self):
        # 합법 전이 체인(R8: update_run도 전이표 경유) — queued→running→done
        R.update_run(self.key, status="running", started=time.time())
        R.update_run(self.key, status="done", exit=0, ended=time.time())

    def test_target_sha_extracts_first_line_only(self):
        self.assertEqual(R.target_sha(self.msg_a), "85d6670a")
        self.assertEqual(R.target_sha(self.msg_b), "07bcc393")
        # harvest 마커 형식(PR#n@sha8)도 커버
        self.assertEqual(R.target_sha("PR#12@abcdef12"), "abcdef12")
        # 대상 아님: sha 없음 / 본문(둘째 줄)의 sha는 인용일 뿐
        self.assertIsNone(R.target_sha("일반 작업 지시"))
        self.assertIsNone(R.target_sha("일반 재지시\nPR #3 (r/x) @ deadbeef 본문 인용"))

    def test_same_id_new_sha_reruns_as_new_round(self):
        self._done()
        session1 = R.get_run(self.key)["session"]
        p2 = dict(self.p1, message=self.msg_b)
        _, fresh = R.prepare(p2)  # RED: 현재 DUP-SKIP으로 False
        self.assertTrue(fresh)
        ent = R.get_run(self.key)
        self.assertEqual(ent["status"], "queued")
        self.assertEqual(ent["message"], self.msg_b)
        self.assertEqual(ent["target_sha"], "07bcc393")
        self.assertEqual(ent["prev_sha"], "85d6670a")
        # 재회차 세션은 이전 회차 세션명과 충돌하지 않아야 한다(keep_shell 생존 대비)
        self.assertNotEqual(ent["session"], session1)
        self.assertTrue(ent["session"].startswith(session1 + "-"))
        self.assertIn("07bcc393", ent["session"])

    def test_same_id_same_sha_still_dup_skip(self):
        self._done()
        _, fresh = R.prepare(dict(self.p1))  # 동일 메시지 재전송
        self.assertFalse(fresh)
        self.assertEqual(R.get_run(self.key)["status"], "done")

    def test_no_target_sha_message_still_dup_skip(self):
        self._done()
        _, fresh = R.prepare(dict(self.p1, message="일반 재지시 — 대상 sha 없음"))
        self.assertFalse(fresh)
        self.assertEqual(R.get_run(self.key)["status"], "done")

    def test_mismatch_while_not_done_flags_visibility_once(self):
        comments = []
        R.tt_comment = lambda issue, body: comments.append((issue, body))
        ent = R.get_run(self.key)  # done→running은 전이표상 불법(R8) — 직접 재시드
        ent["status"] = "running"
        R.save_runs({self.key: ent})  # done이 아니면 재실행 없음
        p2 = dict(self.p1, message=self.msg_b)
        _, fresh = R.prepare(p2)
        self.assertFalse(fresh)
        self.assertEqual(R.get_run(self.key)["status"], "running")
        hits = [b for i, b in comments if i == self.issue and "대상 불일치" in b]
        self.assertEqual(len(hits), 1)  # 가시화(C): 사유에 대상 불일치 명시
        R.prepare(p2)  # 같은 재전송 반복 — 코멘트 중복 없음
        hits = [b for i, b in comments if i == self.issue and "대상 불일치" in b]
        self.assertEqual(len(hits), 1)

    def test_stale_artifacts_cleared_on_reround(self):
        self._done()
        base = os.path.join(R.RUNTIME_DIR, self.key.replace("#", "_"))
        os.makedirs(R.RUNTIME_DIR, exist_ok=True)
        open(base + ".done", "w").close()  # 잔여 마커 — 제거 안 되면 즉시 오판정
        with open(base + ".log", "w") as f:
            f.write("1회차 로그 — 2회차로 유출되면 안 됨")
        p2 = dict(self.p1, message=self.msg_b)
        R.prepare(p2)
        self.assertFalse(os.path.exists(base + ".done"))
        self.assertFalse(os.path.exists(base + ".log"))


class TestRoundSessionContext(unittest.TestCase):
    """PR#47 codex 리뷰 R1/R2 보완 (기준 SHA 0b7d1bf5).

    R1: 재회차의 실제 세션과 prepare 반환값·즉시 200 context가 어긋나면 서버가 이전
    회차 context를 저장해 후속 dispatch가 이전 세션으로 continue한다(STALL 재발).
    R2: 회차명이 base+sha8뿐이면 sha 재방문(A→B→C→B)에서 keep_shell로 생존한 과거
    회차 세션과 충돌한다(tmux duplicate rc=1 → failed → DUP-SKIP).
    """

    DID = 501

    def setUp(self):
        self.n = str(self.id()).rsplit(".", 1)[-1]
        self.issue = "RRND-" + self.n
        self.key = "%s#%d" % (self.issue, self.DID)
        self.msg_a = "[auto review] PR #3 (r/x) @ 85d6670a — 카드 %s 리뷰" % self.issue
        self.msg_b = "[auto review] PR #3 (r/x) @ 07bcc393 — 카드 %s 리뷰" % self.issue
        self.msg_c = "[auto review] PR #3 (r/x) @ c0defeed — 카드 %s 리뷰" % self.issue
        self.orig_alive = R.session_alive

    def tearDown(self):
        R.session_alive = self.orig_alive

    def _p(self, message, did=None, context=""):
        return {"dispatch_id": did or self.DID, "issue_id": self.issue,
                "agent": "p-read", "message": message, "context": context}

    def _done(self):
        # 합법 전이 체인(R8: update_run도 전이표 경유) — queued→running→done
        R.update_run(self.key, status="running", started=time.time())
        R.update_run(self.key, status="done", exit=0, ended=time.time())

    # R1: 장부·prepare 반환값·HTTP 200 context가 현재 회차 세션으로 일치
    def test_reround_session_matches_prepare_and_hook_context(self):
        s1, _ = R.prepare(self._p(self.msg_a))
        self._done()
        R.session_alive = lambda name: True  # keep_shell 생존 시나리오
        p2 = self._p(self.msg_b)
        ctx_200 = R.ctx_token(R.decide_session(p2))  # /hook 즉시 200의 context
        s2, fresh2 = R.prepare(dict(p2, context=ctx_200))
        self.assertTrue(fresh2)
        ent = R.get_run(self.key)
        self.assertEqual(ent["session"], s2)  # 장부 = prepare 반환값
        self.assertEqual(R.parse_ctx(ctx_200), ent["session"])  # 200 = 현재 회차
        self.assertNotEqual(s2, s1)  # 이전 회차 세션 회수 금지
        self.assertIn("07bcc393", s2)

    # R1: 재회차 뒤 후속 dispatch는 현재 회차 세션에서 계속된다(STALL 재발 회귀)
    def test_followup_dispatch_after_reround_lands_in_round_session(self):
        R.prepare(self._p(self.msg_a))
        self._done()
        R.session_alive = lambda name: True
        p2 = self._p(self.msg_b)
        ctx_200 = R.ctx_token(R.decide_session(p2))  # 서버가 저장할 context
        R.prepare(dict(p2, context=ctx_200))  # 재회차 실행
        self._done()
        round_session = R.get_run(self.key)["session"]
        sf, _ = R.prepare(self._p("후속 지시 — 판정 코멘트 확인", did=self.DID + 1,
                                  context=ctx_200))
        self.assertEqual(sf, round_session)
        self.assertEqual(R.parse_ctx(ctx_200), round_session)  # continue 대상 = 재회차 세션

    # R2: A→B→C→B sha 재방문 — 모든 회차명이 서로 충돌하지 않는다
    def test_sha_revisit_gets_unique_round_sessions(self):
        R.session_alive = lambda name: True
        names = []
        for i, m in enumerate((self.msg_a, self.msg_b, self.msg_c, self.msg_b)):
            if i:
                self._done()
            s, fresh = R.prepare(self._p(m))
            self.assertTrue(fresh, "round %d" % (i + 1))
            ent = R.get_run(self.key)
            self.assertEqual(ent["session"], s)  # R1 회귀 방지: 반환 = 장부
            names.append(ent["session"])
            self.assertEqual(ent.get("round"), i + 1)
        self.assertEqual(len(set(names)), 4)  # RED: 2·4회차 이름 충돌


class TestHookClaimFirstContext(unittest.TestCase):
    """PR#47 codex 리뷰 R1 잔여 (기준 SHA d952f721): /hook은 백그라운드 선점 스레드를
    먼저 시작하고(:1153) 그 직후 200 context를 계산한다(:1155) — 선점이 먼저 반영되면
    재회차 조건(done+불일치)이 사라져 입력 ctx의 이전 회차 세션이 응답으로 나간다.
    기존 신규 R1 테스트(:557-582)는 decide_session을 prepare보다 '먼저' 호출하는 순서만
    검사해 이 경로를 놓쳤다. 회차 선점과 반환 context는 선점 전·후·경합 모든 순서에서,
    그리고 queued/running/done·중복 재전송에서도 같아야 한다.
    """

    DID = 601

    def setUp(self):
        self.n = str(self.id()).rsplit(".", 1)[-1]
        self.issue = "HOOK-" + self.n
        self.key = "%s#%d" % (self.issue, self.DID)
        self.msg_a = "[auto review] PR #3 (r/x) @ 85d6670a — 카드 %s 리뷰" % self.issue
        self.msg_b = "[auto review] PR #3 (r/x) @ 07bcc393 — 카드 %s 리뷰" % self.issue
        self.msg_c = "[auto review] PR #3 (r/x) @ c0defeed — 카드 %s 리뷰" % self.issue
        self.orig_alive = R.session_alive
        self.orig_comment = R.tt_comment
        self.comments = []
        R.tt_comment = lambda issue, body: self.comments.append((issue, body))

    def tearDown(self):
        R.session_alive = self.orig_alive
        R.tt_comment = self.orig_comment

    def _p(self, message, did=None, context=""):
        return {"dispatch_id": did or self.DID, "issue_id": self.issue,
                "agent": "p-read", "message": message, "context": context}

    def _done(self):
        # 합법 전이 체인(R8: update_run도 전이표 경유) — queued→running→done
        R.update_run(self.key, status="running", started=time.time())
        R.update_run(self.key, status="done", exit=0, ended=time.time())

    # ① 실제 /hook 선점 선행 순서: claim이 먼저 장부를 바꾼 뒤 200을 계산해도 현재 회차
    def test_hook_claims_first_context_still_current_round(self):
        s1, _ = R.prepare(self._p(self.msg_a))
        self._done()
        R.session_alive = lambda name: True  # keep_shell로 이전 회차 생존
        p2 = self._p(self.msg_b, context=R.ctx_token(s1))  # 서버 저장 ctx = 이전 회차
        s2, fresh = R.prepare(p2)  # 백그라운드 선점이 먼저 — /hook 실제 순서
        self.assertTrue(fresh)
        ent = R.get_run(self.key)
        self.assertEqual(ent["session"], s2)
        ctx_200 = R.ctx_token(R.decide_session(p2))  # 선점 '후' 계산되는 즉시 200
        self.assertEqual(R.parse_ctx(ctx_200), ent["session"])  # RED: 이전 회차 회수됨

    # ① 진짜 스레드 경합: 선점과 200 계산이 어느 순서로 겹쳐도 200 == 선점 세션
    def test_hook_thread_race_context_matches_claimed_round(self):
        R.prepare(self._p(self.msg_a))
        self._done()
        R.session_alive = lambda name: True
        for m in (self.msg_b, self.msg_c, self.msg_b):  # B→C→B 재방문에서도
            p = self._p(m, context=R.ctx_token(R.get_run(self.key)["session"]))
            out = {}

            def hook_seq():
                out["s"], out["fresh"] = R.prepare(p)

            t = threading.Thread(target=hook_seq, daemon=True)
            t.start()
            out["ctx"] = R.decide_session(p)  # 스레드와 경합하며 즉시 계산
            t.join(timeout=5)
            self.assertTrue(out["fresh"])
            self.assertEqual(out["ctx"], out["s"])  # 모든 인터리빙에서 일치
            self.assertEqual(out["ctx"], R.get_run(self.key)["session"])
            self._done()

    # ② 완료 후 원래 요청(A ctx 포함) 재전송 — DUP-SKIP 응답도 현재 회차 세션
    def test_dup_resend_after_done_returns_current_round_session(self):
        s1, _ = R.prepare(self._p(self.msg_a))
        self._done()
        R.session_alive = lambda name: True
        p_b = self._p(self.msg_b, context=R.ctx_token(s1))
        R.prepare(p_b)  # B 회차 선점·실행
        self._done()
        s2, fresh = R.prepare(p_b)  # 원래 B 요청 재전송 → DUP-SKIP
        self.assertFalse(fresh)
        ctx_200 = R.ctx_token(R.decide_session(p_b))
        self.assertEqual(R.parse_ctx(ctx_200), s2)  # = 장부에 선점된 세션
        self.assertNotEqual(R.parse_ctx(ctx_200), s1)  # RED: A가 회수됨

    # ② 진행 중 대상 불일치 재전송 — queued/running에서도 장부 세션이 응답 기준
    def test_mismatch_resend_while_running_returns_claimed_session(self):
        s1, _ = R.prepare(self._p(self.msg_a))
        self._done()
        R.session_alive = lambda name: True
        p_b = self._p(self.msg_b, context=R.ctx_token(s1))
        R.prepare(p_b)  # B 선점 → queued
        R.update_run(self.key, status="running")
        s2, fresh = R.prepare(p_b)  # 진행 중 재전송 — DUP-SKIP(가시화 1회)
        self.assertFalse(fresh)
        self.assertEqual(R.get_run(self.key)["status"], "running")
        ctx_200 = R.ctx_token(R.decide_session(p_b))
        self.assertEqual(R.parse_ctx(ctx_200), s2)
        self.assertNotEqual(R.parse_ctx(ctx_200), s1)  # RED: A가 회수됨

    # ③ 선점 선행 순서의 재회차 뒤 후속 dispatch도 현재 회차에서 계속된다
    def test_followup_after_claim_first_reround_continues_round(self):
        s1, _ = R.prepare(self._p(self.msg_a))
        self._done()
        R.session_alive = lambda name: True
        p2 = self._p(self.msg_b, context=R.ctx_token(s1))
        R.prepare(p2)  # 선점 먼저 — 실제 /hook 순서
        ctx_200 = R.ctx_token(R.decide_session(p2))  # 서버가 저장할 200 context
        self._done()
        sf, fresh_f = R.prepare(self._p("후속 지시 — 판정 코멘트 확인", did=self.DID + 1,
                                        context=ctx_200))
        self.assertTrue(fresh_f)
        self.assertEqual(sf, R.get_run(self.key)["session"])  # RED: 이전 회차로 continue
        self.assertEqual(R.parse_ctx(ctx_200), sf)

    # 실제 HTTP /hook 엔드포인트: 스레드 선점 → 즉시 200 — 응답 context == 장부 세션
    def test_real_hook_endpoint_context_matches_claimed_session(self):
        s1, _ = R.prepare(self._p(self.msg_a))
        self._done()
        R.session_alive = lambda name: True
        executed = threading.Event()
        orig_exec = R.execute_once
        R.execute_once = lambda p, s: executed.set()
        try:
            srv = R.ThreadingHTTPServer(("127.0.0.1", 0), R.H)
            th = threading.Thread(target=srv.serve_forever, daemon=True)
            th.start()
            try:
                conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1],
                                                  timeout=10)
                body = json.dumps({"dispatch_id": self.DID, "issue_id": self.issue,
                                   "agent": "p-read", "message": self.msg_b,
                                   "context": R.ctx_token(s1)})
                conn.request("POST", "/hook", body=body, headers={
                    "Authorization": "Bearer test-secret-value",
                    "X-Tt-Dispatch": "1", "Content-Type": "application/json"})
                resp = conn.getresponse()
                self.assertEqual(resp.status, 200)
                ctx_200 = json.loads(resp.read()).get("context")
                conn.close()
            finally:
                srv.shutdown()
                srv.server_close()
            self.assertTrue(executed.wait(timeout=5), "백그라운드 선점·실행 미완료")
            self.assertEqual(R.parse_ctx(ctx_200), R.get_run(self.key)["session"])
        finally:
            R.execute_once = orig_exec


class TestCompletionTransitionRace(unittest.TestCase):
    """PR#47 codex 3차 판정 R1 잔여 (기준 SHA ac5b7b0f): 완료 전환과 겹치는 선점.

    A의 .done 마커는 이미 존재하지만 장부는 running(감시자 finalize 대기)일 때
    재전송 B가 오면, 응답 결정(장부 running → A 세션)과 백그라운드
    claim(done+새 SHA → B-r2 선점)이 서로 다른 시점·lock의 장부 읽기를 하므로
    200 != 실제 회차가 된다. 요구: 응답 결정과 claim을 동기화 — 응답과 실행이
    '동일한 선점 결과'를 공유하고, 완료 감시자의 running→done 전환은 그 결정에
    흡수된다. 기존 경합 테스트(TestHookClaimFirstContext)는 기존 회차를 미리
    done으로 만들고 시작해 이 전환을 놓쳤다.
    """

    DID = 701

    def setUp(self):
        self.n = str(self.id()).rsplit(".", 1)[-1]
        self.issue = "DONE-" + self.n
        self.key = "%s#%d" % (self.issue, self.DID)
        self.msg_a = "[auto review] PR #3 (r/x) @ 85d6670a — 카드 %s 리뷰" % self.issue
        self.msg_b = "[auto review] PR #3 (r/x) @ 07bcc393 — 카드 %s 리뷰" % self.issue
        self.orig_alive = R.session_alive
        self.orig_exec = R.execute_once
        self.orig_comment = R.tt_comment
        self.orig_prog = R.tt_progress
        self.comments = []
        self.executed = []
        R.tt_comment = lambda issue, body: self.comments.append((issue, body))
        # 통합 접수 시뮬레이션(R3): 종료 보고는 progress payload의 comment로 도착
        def _prog(iid, did, payload):
            if payload.get("comment"):
                self.comments.append((str(iid), payload["comment"]))
            return True
        R.tt_progress = _prog

    def tearDown(self):
        R.session_alive = self.orig_alive
        R.execute_once = self.orig_exec
        R.tt_comment = self.orig_comment
        R.tt_progress = self.orig_prog

    def _p(self, message, did=None, context=""):
        return {"dispatch_id": did or self.DID, "issue_id": self.issue,
                "agent": "p-read", "message": message, "context": context}

    def _mark_done(self):
        """감시자가 아직 finalize하지 않은 물리적 완료 — .done/.exit 마커만 기록."""
        base = os.path.join(R.RUNTIME_DIR, self.key.replace("#", "_"))
        with open(base + ".exit", "w") as f:
            f.write("0")
        open(base + ".done", "w").close()

    def _hook(self, payload):
        """실제 /hook 호출 — (200 context, 서버 shutdown) 반환."""
        srv = R.ThreadingHTTPServer(("127.0.0.1", 0), R.H)
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1],
                                              timeout=10)
            body = json.dumps(payload)
            conn.request("POST", "/hook", body=body, headers={
                "Authorization": "Bearer test-secret-value",
                "X-Tt-Dispatch": "1", "Content-Type": "application/json"})
            resp = conn.getresponse()
            status = resp.status
            ctx_200 = json.loads(resp.read()).get("context")
            conn.close()
            return status, ctx_200
        finally:
            srv.shutdown()
            srv.server_close()

    # ① codex 재현: 마커는 있는데 장부가 running — 선점이 전환을 흡수해 응답==실행==재회차
    def test_pending_completion_absorbed_response_matches_preempted_round(self):
        s1, _ = R.prepare(self._p(self.msg_a))
        R.update_run(self.key, status="running", started=time.time())
        self._mark_done()  # 물리 완료 — 감시자 finalize 전(장부는 running)
        R.session_alive = lambda name: True  # keep_shell 생존 이전 회차
        executed = threading.Event()

        def _exec(p, s):
            self.executed.append(s)
            executed.set()
        R.execute_once = _exec
        status, ctx_200 = self._hook(self._p(self.msg_b, context=R.ctx_token(s1)))
        self.assertEqual(status, 200)
        self.assertTrue(executed.wait(timeout=5), "백그라운드 실행 미완료")
        ent = R.get_run(self.key)
        self.assertEqual(ent["status"], "queued")  # 재회차 선점됨
        self.assertIn("07bcc393", ent["session"])  # RED: 옛 구조는 재회차 자체가 없음
        self.assertIn("-r2", ent["session"])
        self.assertEqual(R.parse_ctx(ctx_200), ent["session"])  # RED: 200 == A 세션
        self.assertEqual(self.executed[-1], ent["session"])  # 실행도 동일 선점 결과
        self.assertNotEqual(R.parse_ctx(ctx_200), s1)  # 이전 회차 회수 금지

    # ② 흡수된 finalize가 정확히 1회 보고되고 이후 감시자 tick이 이중 처리하지 않는다
    def test_absorbed_finalize_reports_once_and_watch_does_not_duplicate(self):
        s1, _ = R.prepare(self._p(self.msg_a))
        R.update_run(self.key, status="running", started=time.time())
        self._mark_done()
        R.session_alive = lambda name: True
        executed = threading.Event()
        R.execute_once = lambda p, s: (self.executed.append(s), executed.set())
        self._hook(self._p(self.msg_b))
        self.assertTrue(executed.wait(timeout=5))
        done_comments = [c for c in self.comments if "done exit=0" in c[1]]
        self.assertEqual(len(done_comments), 1)  # 흡수된 finalize 보고
        self.assertIn(s1, done_comments[0][1])  # 전환된 회차(A) 기준
        R.watch_once()  # 감시자 tick — 흡수된 전환을 재처리하지 않는다
        self.assertEqual(len([c for c in self.comments if "done exit=0" in c[1]]), 1)

    # ③ 감시자가 먼저 tick한 경우(실제 finalize)에도 응답==실행==재회차
    def test_watcher_ticked_before_preempt_response_matches_round(self):
        R.prepare(self._p(self.msg_a))
        R.update_run(self.key, status="running", started=time.time())
        self._mark_done()
        R.watch_once()  # 감시자 먼저 — 장부 done 전환
        self.assertEqual(R.get_run(self.key)["status"], "done")
        R.session_alive = lambda name: True
        executed = threading.Event()
        R.execute_once = lambda p, s: (self.executed.append(s), executed.set())
        status, ctx_200 = self._hook(self._p(self.msg_b))
        self.assertEqual(status, 200)
        self.assertTrue(executed.wait(timeout=5))
        ent = R.get_run(self.key)
        self.assertEqual(R.parse_ctx(ctx_200), ent["session"])
        self.assertEqual(self.executed[-1], ent["session"])
        self.assertIn("-r2", ent["session"])

    # ④ 선점 시점에 마커가 없으면(진짜 running) 단일 결정은 DUP-SKIP — 응답·실행·
    #    이후 전환·후속 dispatch가 모두 그 결정에 일관된다
    def test_completion_after_preempt_keeps_single_decision_consistent(self):
        s1, _ = R.prepare(self._p(self.msg_a))
        R.update_run(self.key, status="running", started=time.time())
        R.session_alive = lambda name: True
        executed = threading.Event()
        R.execute_once = lambda p, s: (self.executed.append(s), executed.set())
        status, ctx_200 = self._hook(self._p(self.msg_b))
        self.assertEqual(status, 200)
        self.assertEqual(R.parse_ctx(ctx_200), s1)  # 진행 중 — 현재 회차 유지
        self.assertFalse(executed.wait(timeout=0.5))  # DUP-SKIP — 실행 없음
        self._mark_done()
        R.watch_once()  # 전환은 선점 이후 — 정상 finalize
        self.assertEqual(R.get_run(self.key)["status"], "done")
        # 응답 context를 쓰는 후속 dispatch: 응답이 가리킨 세션으로 일관 continue
        sf, _ = R.prepare(self._p("후속 지시 — 판정 코멘트 확인", did=self.DID + 1,
                                  context=ctx_200))
        self.assertEqual(sf, s1)

    # ⑤ 승인 메시지는 선점하지 않는다 — held 런 해제만, 장부 오염 금지
    def test_approve_message_does_not_claim_ledger(self):
        s1, _ = R.prepare(self._p(self.msg_a))
        R.update_run(self.key, status="held", gate="rm -rf")
        executed = threading.Event()
        R.execute_once = lambda p, s: (self.executed.append(s), executed.set())
        approve_key = "%s#%d" % (self.issue, self.DID + 9)
        status, ctx_200 = self._hook(self._p("승인", did=self.DID + 9,
                                             context=R.ctx_token(s1)))
        self.assertEqual(status, 200)
        self.assertTrue(executed.wait(timeout=5))  # 원 메시지 실행
        self.assertIsNone(R.get_run(approve_key))  # 승인 dispatch로 선점 생성 금지
        self.assertEqual(R.get_run(self.key)["session"], s1)  # held 런 그대로 실행


class TestFinalizeOwnershipGate(unittest.TestCase):
    """PR#47 codex 4차 판정 R3 (기준 SHA 8e2e43f5): 이전 회차 감시자가 현재 회차를 덮어씀.

    watch_once가 A 스냅샷으로 finalize에 들어가 TT 보고(네트워크)에서 대기하는 동안
    B /hook이 흡수+선점하면, finalize의 update_run(done)이 회차 확인 없이 현재 B
    엔트리를 덮는다 — ① B 세션 미생성 + 동일 B 재전송 영구 DUP-SKIP, ② B 실행 후
    반환이면 .done 마커 없이 running→done 되덮어 B 완료 보고 0건, 그리고 A 완료
    코멘트는 감시자+흡수 경로가 각각 보고해 2건 중복. 요구(codex): 완료 전환·보고
    소유권을 감시자와 흡수 경로가 같은 원자적 게이트에서 결정 — finalize는 관찰한
    session/round를 lock 안에서 확인하고 네트워크 호출은 lock 밖에 둔다. 기존 신규
    테스트(TestCompletionTransitionRace)는 watch_once를 hook 후 또는 완전 종료 뒤에
    실행해 finalize 내부 대기(중첩)를 재현하지 못했다.
    """

    DID = 801

    def setUp(self):
        self.n = str(self.id()).rsplit(".", 1)[-1]
        self.issue = "OWN-" + self.n
        self.key = "%s#%d" % (self.issue, self.DID)
        self.msg_a = "[auto review] PR #3 (r/x) @ 85d6670a — 카드 %s 리뷰" % self.issue
        self.msg_b = "[auto review] PR #3 (r/x) @ 07bcc393 — 카드 %s 리뷰" % self.issue
        self.orig_alive = R.session_alive
        self.orig_exec = R.execute_once
        self.orig_comment = R.tt_comment
        self.orig_prog = R.tt_progress
        # 통합 접수 시뮬레이션(R3): 종료 보고는 progress payload의 comment로 도착
        def _prog(iid, did, payload):
            if payload.get("comment"):
                self.comments.append((str(iid), payload["comment"]))
            return True
        R.tt_progress = _prog
        self.comments = []  # 전달(반환)된 코멘트만 기록
        self.executed = []
        self.gate_entered = threading.Event()
        self.gate_release = threading.Event()
        R.session_alive = lambda name: True
        R.execute_once = lambda p, s: self.executed.append(s)

    def tearDown(self):
        R.session_alive = self.orig_alive
        R.execute_once = self.orig_exec
        R.tt_comment = self.orig_comment

    def _p(self, message, did=None, context=""):
        return {"dispatch_id": did or self.DID, "issue_id": self.issue,
                "agent": "p-read", "message": message, "context": context}

    def _prepare_running_a(self):
        """A 회차 running + 물리 완료 마커 — 감시자가 아직 finalize하지 않은 상태."""
        s1, _ = R.prepare(self._p(self.msg_a))
        R.update_run(self.key, status="running", started=time.time())
        base = os.path.join(R.RUNTIME_DIR, self.key.replace("#", "_"))
        with open(base + ".exit", "w") as f:
            f.write("0")
        open(base + ".done", "w").close()
        return s1

    def _gate_comment(self, issue, body):
        """finalize 내부 대기 재현: 이 카드의 보고 네트워크 호출에서 블록.
        (R3 — 보고는 progress 경유이므로 tt_progress 게이트 _gate_prog 사용)"""
        self.gate_entered.set()
        self.gate_release.wait(timeout=10)
        self.comments.append((issue, body))

    def _gate_prog(self, iid, did, payload):
        """finalize 내부 대기 재현 — 통합 보고(progress) 호출에서 블록."""
        if str(did) == str(self.DID) and payload.get("comment"):
            self.gate_entered.set()
            self.gate_release.wait(timeout=10)
            self.comments.append((str(iid), payload["comment"]))
        return True

    def _recorder_comment(self, issue, body):
        self.comments.append((issue, body))

    def _hook(self, payload):
        """실제 /hook 엔드포인트 호출 — (status, 200 context) 반환."""
        srv = R.ThreadingHTTPServer(("127.0.0.1", 0), R.H)
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1],
                                              timeout=10)
            body = json.dumps(payload)
            conn.request("POST", "/hook", body=body, headers={
                "Authorization": "Bearer test-secret-value",
                "X-Tt-Dispatch": "1", "Content-Type": "application/json"})
            resp = conn.getresponse()
            status = resp.status
            ctx_200 = json.loads(resp.read()).get("context")
            conn.close()
            return status, ctx_200
        finally:
            srv.shutdown()
            srv.server_close()

    def _mark_b_done(self):
        base = os.path.join(R.RUNTIME_DIR, self.key.replace("#", "_"))
        with open(base + ".exit", "w") as f:
            f.write("0")
        open(base + ".done", "w").close()

    def _done_of(self, session):
        return [b for _, b in self.comments
                if "done exit=0" in b and ("session=%s\n" % session) in b]

    # ① codex 재현 경로 1: 감시자가 보고 호출에서 대기 중 B 선점 → 늦은 반환
    def test_late_watcher_return_does_not_clobber_preempted_round(self):
        s1 = self._prepare_running_a()
        R.tt_progress = self._gate_prog
        wt = threading.Thread(target=R.watch_once, daemon=True)
        wt.start()
        self.assertTrue(self.gate_entered.wait(timeout=5),
                        "감시자가 finalize의 TT 보고 호출에 진입하지 못함")
        status, ctx_200 = self._hook(self._p(self.msg_b, context=R.ctx_token(s1)))
        self.assertEqual(status, 200)  # B 선점 정상 — 200 context는 r2
        self.gate_release.set()
        wt.join(timeout=10)
        self.assertFalse(wt.is_alive(), "감시자 반환 지연")
        ent = R.get_run(self.key)
        # RED: 구조는 finalize의 update_run(done)이 B(queued)를 덮어 세션 미생성
        self.assertEqual(ent["status"], "queued")
        self.assertEqual(ent["round"], 2)
        self.assertIn("-r2", ent["session"])
        self.assertEqual(R.parse_ctx(ctx_200), ent["session"])
        # A 완료 보고 정확 1건 — 감시자 소유, 흡수 경로 중복 없음 (RED: 구조는 2건)
        self.assertEqual(len(self._done_of(s1)), 1)
        # 동일 요청 재시도 — 여전히 현재 회차(B) 응답, 상태 훼손 없음
        status2, ctx2 = self._hook(self._p(self.msg_b, context=R.ctx_token(s1)))
        self.assertEqual(status2, 200)
        self.assertEqual(R.parse_ctx(ctx2), ent["session"])
        self.assertEqual(R.get_run(self.key)["status"], "queued")
        # B 실행+완료 → 완료 보고 1건 (RED: 구조는 B가 done으로 덮인 뒤라 보고 0건)
        R.tt_comment = self._recorder_comment
        R.update_run(self.key, status="running", started=time.time())
        self._mark_b_done()
        R.watch_once()
        self.assertEqual(len(self._done_of(ent["session"])), 1)
        self.assertEqual(R.get_run(self.key)["status"], "done")

    # ② codex 재현 경로 2: B 실행 중에 감시자 반환 — running 되덮기 금지 + B 보고 생존
    def test_late_watcher_return_after_b_started_keeps_round_and_report(self):
        s1 = self._prepare_running_a()
        R.tt_progress = self._gate_prog
        wt = threading.Thread(target=R.watch_once, daemon=True)
        wt.start()
        self.assertTrue(self.gate_entered.wait(timeout=5),
                        "감시자가 finalize의 TT 보고 호출에 진입하지 못함")
        status, _ = self._hook(self._p(self.msg_b, context=R.ctx_token(s1)))
        self.assertEqual(status, 200)
        # B 실제 실행 개시 (execute_once의 queued→running 전환과 동일)
        R.update_run(self.key, status="running", started=time.time())
        self.gate_release.set()
        wt.join(timeout=10)
        self.assertFalse(wt.is_alive())
        # RED: 구조는 update_run(done)이 .done 마커 없이 실행 중인 B를 되덮는다
        ent = R.get_run(self.key)
        self.assertEqual(ent["status"], "running")
        self.assertEqual(ent["round"], 2)
        # A 완료 보고 1건 — 중복 없음 (RED: 구조는 감시자+흡수 2건)
        self.assertEqual(len(self._done_of(s1)), 1)
        # B 정상 종료 → 완료 보고 1건 (RED: 구조는 감시자가 건너뛰어 0건)
        R.tt_comment = self._recorder_comment
        self._mark_b_done()
        R.watch_once()
        self.assertEqual(len(self._done_of(ent["session"])), 1)
        self.assertEqual(R.get_run(self.key)["status"], "done")

    # ③ 흡수 경로가 보고 소유 — 이후 진입한 늦은 감시자는 중복 보고·덮기 없음
    def test_absorb_path_owns_report_late_finalize_skipped(self):
        s1 = self._prepare_running_a()
        stale_snap = R.get_run(self.key)  # 선점 이전 A 회차 스냅샷 (중첩 감시자 보유분)
        R.tt_comment = self._recorder_comment
        executed = threading.Event()
        R.execute_once = lambda p, s: (self.executed.append(s), executed.set())
        status, _ = self._hook(self._p(self.msg_b, context=R.ctx_token(s1)))
        self.assertEqual(status, 200)
        self.assertTrue(executed.wait(timeout=5))  # 백그라운드 _safe: run_deferred 선행
        # 흡수 경로 보고 1건 (write=False)
        self.assertEqual(len(self._done_of(s1)), 1)
        # 늦은 감시자 — 이전 회차 스냅샷으로 finalize 재진입 (RED: 구조는 재보고+되덮기)
        outcome = R.finalize(None, self.key, stale_snap, 0)
        self.assertEqual(outcome, "skipped")
        self.assertEqual(len(self._done_of(s1)), 1)
        self.assertEqual(R.get_run(self.key)["status"], "queued")  # B 무훼손

    # ④ 게이트 단위 계약: 보고 소유 단일화 + 관찰 회차 불일치 전환 거부
    def test_ownership_gate_unit_semantics(self):
        s1, _ = R.prepare(self._p(self.msg_a))
        R.update_run(self.key, status="running", started=time.time())
        snap = R.get_run(self.key)
        self.assertTrue(R.claim_report(self.key, snap, 0))   # 선점 — 단일 소유
        self.assertFalse(R.claim_report(self.key, snap, 0))  # 중복 claim 거부
        self.assertTrue(R.update_run_if_round(self.key, snap, status="done", exit=0))
        self.assertEqual(R.get_run(self.key)["status"], "done")
        # 다음 회차 교체 후 — 이전 회차의 전환·완료 claim은 거부, 엔트리 무훼손
        # (done→running은 전이표상 불법(R8) — 회차 교체 시드는 직접 기록)
        ent2 = R.get_run(self.key)
        ent2.update({"status": "running", "round": 2,
                     "session": s1 + "-07bcc393-r2", "claimed_report": None})
        R.save_runs({self.key: ent2})
        self.assertFalse(R.update_run_if_round(self.key, snap, status="failed", exit=1))
        ent = R.get_run(self.key)
        self.assertEqual(ent["status"], "running")
        self.assertEqual(ent["round"], 2)
        self.assertFalse(R.claim_report(self.key, snap, 0))  # 완료 보고는 흡수 경로 소유
        self.assertTrue(R.claim_report(self.key, snap, 1))   # 실패 보고는 감시자 몫


class TestStopAndUpdateRunGuards(unittest.TestCase):
    """리뷰 R8 — 상태 쓰기의 유일 결정 지점 강제."""

    KEY = "T-8#801"

    def test_update_run_rejects_illegal_status_transition(self):
        R.save_runs({self.KEY: {"status": "done", "issue_id": "T-8", "dispatch_id": 801}})
        R.update_run(self.KEY, status="running", started=time.time())  # done→running 불법
        ent = R.get_run(self.KEY)
        self.assertEqual(ent["status"], "done")  # 무훼손
        self.assertNotIn("started", ent)  # 절반 적용도 없음

    def test_update_run_allows_legal_transition_and_fields(self):
        R.save_runs({self.KEY: {"status": "queued", "issue_id": "T-8", "dispatch_id": 801}})
        R.update_run(self.KEY, status="running", started=time.time())
        ent = R.get_run(self.KEY)
        self.assertEqual(ent["status"], "running")
        self.assertIn("started", ent)

    def test_cmd_stop_on_terminal_entry_keeps_state(self):
        R.save_runs({self.KEY: {"status": "done", "issue_id": "T-8",
                                "dispatch_id": 801, "session": "tt-x-801"}})
        orig = R.tmux_run
        try:
            R.tmux_run = lambda *a, **k: subprocess.CompletedProcess([], 0)
            R.cmd_stop(self.KEY)
        finally:
            R.tmux_run = orig
        self.assertEqual(R.get_run(self.KEY)["status"], "done")  # 종단 재지정 없음

    def test_cmd_stop_on_running_entry_transitions_via_table(self):
        R.save_runs({self.KEY: {"status": "running", "issue_id": "T-8",
                                "dispatch_id": 801, "session": "tt-x-801"}})
        orig = R.tmux_run
        try:
            R.tmux_run = lambda *a, **k: subprocess.CompletedProcess([], 0)
            R.cmd_stop(self.KEY)
        finally:
            R.tmux_run = orig
        self.assertEqual(R.get_run(self.KEY)["status"], "failed")
        self.assertEqual(R.get_run(self.KEY).get("detail"), "stopped-by-cli")


class TestRunStateMachine(unittest.TestCase):
    """TT 개선#1 (M4DEDPK2-0VSF) 요구 1 — 상태기계 전이표 단위 테스트."""

    S = R.run_state

    def test_table_covers_all_states(self):
        self.assertEqual(set(self.S.TRANSITIONS), self.S.STATES)

    def test_happy_path_queued_to_done(self):
        ent = {"status": "queued"}
        self.S.transition(ent, "running")
        self.S.transition(ent, "stalled")
        self.S.transition(ent, "running")
        self.S.transition(ent, "finalizing")
        self.S.transition(ent, "done", exit=0)
        self.assertEqual(ent["status"], "done")
        self.assertEqual(ent["exit"], 0)

    def test_terminal_absorbing(self):
        for t in self.S.TERMINAL:
            with self.assertRaises(self.S.IllegalTransition):
                self.S.transition({"status": t}, "running")

    def test_queued_direct_done_illegal(self):
        with self.assertRaises(self.S.IllegalTransition):
            self.S.transition({"status": "queued"}, "done")

    def test_entry_untouched_on_illegal(self):
        ent = {"status": "queued", "issue_id": "X-1"}
        with self.assertRaises(self.S.IllegalTransition):
            self.S.transition(ent, "done")
        self.assertEqual(ent, {"status": "queued", "issue_id": "X-1"})

    def test_unknown_current_state_rejected(self):
        with self.assertRaises(self.S.IllegalTransition):
            self.S.transition({"status": "mystery"}, "running")

    def test_stalled_recovery_and_kill_paths(self):
        self.S.transition({"status": "running", "a": 1}, "stalled")
        self.S.transition({"status": "stalled"}, "running")
        self.S.transition({"status": "stalled"}, "finalizing")
        self.S.transition({"status": "stalled"}, "failed", detail="stall-killed")

    def test_active_includes_new_states(self):
        self.assertIn("stalled", self.S.ACTIVE)
        self.assertIn("finalizing", self.S.ACTIVE)
        self.assertNotIn("done", self.S.ACTIVE)


class TestStalledLedgerState(unittest.TestCase):
    """TT 개선#1 요구 1·4 — stalled 장부 상태와 완료 보고 상호작용."""

    def setUp(self):
        self.key = "STL-X#911"
        self.issue = "STL-X"
        self.orig_comment = R.tt_comment
        self.orig_tmux = R.tmux_run
        self.comments = []
        R.tt_comment = lambda i, b: self.comments.append((i, b))
        R.tmux_run = lambda *a, **k: subprocess.CompletedProcess(a, 0)

    def tearDown(self):
        R.tt_comment = self.orig_comment
        R.tmux_run = self.orig_tmux

    def test_stalled_entry_still_reportable(self):
        R.save_runs({self.key: {"status": "stalled", "issue_id": self.issue,
                                "dispatch_id": 911, "session": "tt-stl-911",
                                "round": 1, "stall_notified": True}})
        snap = R.get_run(self.key)
        # stalled에서도 물리 완료 보고 가능 (REPORTABLE)
        self.assertTrue(R.update_run_if_round(self.key, snap, status="done",
                                              exit=0, ended=time.time()))
        self.assertEqual(R.get_run(self.key)["status"], "done")


class TestLateDoneRoundRegression(unittest.TestCase):
    """TT 개선#1 검증 기준 — 이전 회차 감시자의 늦은 done 쓰기 거부 (A→B→C→B 경계).

    finalize는 보고를 lock 밖 네트워크 호출로 하므로 반환 시점이 늦다. 그 사이
    재회차 선점(A→B→C, 대상 재방문 C→B)이 장부를 교체했으면 이전 회차 스냅샷의
    update_run_if_round는 반드시 거부된다(409 CAS와 이중 방어).
    """

    ISSUE = "ABC-1"
    KEY = "ABC-1#701"

    def setUp(self):
        self.orig_comment = R.tt_comment
        R.tt_comment = lambda i, b: None

    def tearDown(self):
        R.tt_comment = self.orig_comment

    def _seed(self, round_no, sha, session):
        R.save_runs({self.KEY: {"status": "running", "issue_id": self.ISSUE,
                                "dispatch_id": 701, "session": session,
                                "target_sha": sha, "round": round_no}})
        return R.get_run(self.KEY)

    def test_round_a_late_done_rejected_at_b(self):
        snap_a = self._seed(1, "aaaaaaaa", "tt-abc-a")
        self._seed(2, "bbbbbbbb", "tt-abc-b-r2")
        self.assertFalse(R.update_run_if_round(self.KEY, snap_a, status="done",
                                               exit=0, ended=time.time()))
        self.assertEqual(R.get_run(self.KEY)["round"], 2)
        self.assertEqual(R.get_run(self.KEY)["status"], "running")

    def test_round_b_late_done_rejected_at_c(self):
        snap_b = self._seed(2, "bbbbbbbb", "tt-abc-b-r2")
        self._seed(3, "cccccccc", "tt-abc-c-r3")
        self.assertFalse(R.update_run_if_round(self.KEY, snap_b, status="done",
                                               exit=0, ended=time.time()))
        self.assertEqual(R.get_run(self.KEY)["round"], 3)

    def test_revisited_target_round4_rejects_round2_write(self):
        # A→B→C→B 경계: 대상 sha 재방문으로 round 4(대상 b)가 되어도
        # round 2 스냅샷의 늦은 done은 회차 비교로 거부된다.
        snap_b = self._seed(2, "bbbbbbbb", "tt-abc-b-r2")
        self._seed(4, "bbbbbbbb", "tt-abc-b-r4")
        self.assertFalse(R.update_run_if_round(self.KEY, snap_b, status="done",
                                               exit=0, ended=time.time()))
        self.assertEqual(R.get_run(self.KEY)["round"], 4)

    def test_current_round_done_accepted(self):
        snap_c = self._seed(3, "cccccccc", "tt-abc-c-r3")
        self.assertTrue(R.update_run_if_round(self.KEY, snap_c, status="done",
                                              exit=0, ended=time.time()))
        self.assertEqual(R.get_run(self.KEY)["status"], "done")

    def test_claim_report_enters_finalizing_once(self):
        self._seed(1, "aaaaaaaa", "tt-abc-a")
        snap = R.get_run(self.KEY)
        self.assertTrue(R.claim_report(self.KEY, snap, 0))
        ent = R.get_run(self.KEY)
        self.assertEqual(ent["status"], "finalizing")
        self.assertEqual(ent["claimed_report"], 1)
        # 동일 회차 재 claim — 거부
        self.assertFalse(R.claim_report(self.KEY, snap, 0))

    def test_finalizing_entry_replaced_by_preempt_rejects_late_write(self):
        snap = self._seed(1, "aaaaaaaa", "tt-abc-a")
        R.claim_report(self.KEY, snap, 0)  # finalizing 진입
        self._seed(2, "bbbbbbbb", "tt-abc-b-r2")  # 선점이 엔트리 교체
        self.assertFalse(R.update_run_if_round(self.KEY, snap, status="done",
                                               exit=0, ended=time.time()))
        self.assertEqual(R.get_run(self.KEY)["round"], 2)

    def test_release_covers_stalled_and_finalizing(self):
        self._seed(1, "aaaaaaaa", "tt-abc-a")
        R.claim_report(self.KEY, R.get_run(self.KEY), 0)  # finalizing
        R.release_issue(self.ISSUE, "test")
        self.assertEqual(R.get_run(self.KEY)["status"], "cancelled")



class TestStaleReportSuppression(unittest.TestCase):
    """TT 개선#1 요구 2 — 서버 CAS 409(stale) 시 완료 보고 억제 + 로컬 조용한 종결.

    finalize는 투영(CAS) 선(先) → 코멘트 후(後) 순서다. 409는 "이 회차는 이미
    재점유됐다"는 서버 최종 판정 — 코멘트·장부 보고를 억제하고 물리 종료만 기록.
    기타 실패(타임아웃·오프라인)는 기존 best-effort대로 보고를 진행한다."""

    ISSUE = "STALE-1"
    KEY = "STALE-1#801"

    def setUp(self):
        self.orig_comment = R.tt_comment
        self.orig_prog = R.tt_progress
        self.comments = []
        self.prog = [True]
        self.prog_calls = []
        R.tt_comment = lambda i, b: self.comments.append((i, b))

        def _prog(*a, **k):
            self.prog_calls.append(k.get("comment") if "comment" in k else (a[2] if len(a) > 2 else None))
            return self.prog.pop(0) if len(self.prog) > 1 else self.prog[0]

        R.tt_progress = _prog

    def tearDown(self):
        R.tt_comment = self.orig_comment
        R.tt_progress = self.orig_prog

    def _seed(self, status="running", round_no=1):
        R.save_runs({self.KEY: {"status": status, "issue_id": self.ISSUE,
                                "dispatch_id": 801, "session": "tt-stale-801",
                                "round": round_no}})
        base = os.path.join(R.RUNTIME_DIR, self.KEY.replace("#", "_"))
        with open(base + ".log", "w") as f:
            f.write("작업 완료 출력")
        return R.get_run(self.KEY)

    def test_stale_409_suppresses_done_comment_and_marks_ledger(self):
        snap = self._seed()
        self.prog = ["stale"]
        outcome = R.finalize(None, self.KEY, snap, 0)
        self.assertEqual(outcome, "stale")
        self.assertEqual(self.comments, [])  # 코멘트 억제
        ent = R.get_run(self.KEY)
        self.assertEqual(ent["status"], "done")
        self.assertEqual(ent["detail"], "stale-suppressed")

    def test_stale_409_suppresses_failed_report(self):
        snap = self._seed()
        self.prog = ["stale"]
        outcome = R.finalize(None, self.KEY, snap, 1, tail="segfault")
        self.assertEqual(outcome, "stale")
        self.assertEqual(self.comments, [])
        self.assertEqual(R.get_run(self.KEY)["detail"], "stale-suppressed")

    def test_network_failure_still_reports_best_effort(self):
        snap = self._seed()
        self.prog = [False]  # 타임아웃·오프라인 — 보고는 pending 보관 후 재전송(R3)
        outcome = R.finalize(None, self.KEY, snap, 0)
        self.assertEqual(outcome, "done")
        self.assertEqual(self.comments, [])  # CAS 우회 폴백 없음
        self.assertTrue(os.path.exists(R._pending_path(self.KEY)))
        self.prog = [True]  # 서버 회복 — CAS 경유 재전송
        R._flush_pending_reports()
        self.assertFalse(os.path.exists(R._pending_path(self.KEY)))
        self.assertEqual(R.get_run(self.KEY)["status"], "done")

    def test_replaced_entry_rejects_stale_terminal_write(self):
        self._seed()
        snap = R.get_run(self.KEY)
        self.prog = ["stale"]
        R.finalize(None, self.KEY, snap, 0, write=False)  # 보고만 — 장부 무시
        # 재회차 선점이 엔트리를 교체한 뒤 늦은 억제 종결 시도 → 전이표가 거부
        R.save_runs({self.KEY: {"status": "queued", "issue_id": self.ISSUE,
                                "dispatch_id": 801, "session": "tt-stale-801-r2",
                                "round": 2}})
        self.prog = ["stale"]
        outcome = R.finalize(None, self.KEY, R.get_run(self.KEY), 0)
        self.assertEqual(outcome, "stale")
        self.assertEqual(R.get_run(self.KEY)["status"], "queued")  # 훼손 없음


class TestReviewEdgeRegressions(unittest.TestCase):
    """codex 리뷰(#59 R1·R3·R6) 회귀 — 경합·장애 경계."""

    ISSUE = "EDGE-1"
    KEY = "EDGE-1#901"

    def setUp(self):
        self.orig_comment = R.tt_comment
        self.orig_prog = R.tt_progress
        self.orig_raw = R.tt_http_raw
        self.comments = []
        self.prog = [True]
        self.prog_calls = []
        R.tt_comment = lambda i, b: self.comments.append((i, b))

        def _prog(*a, **k):
            self.prog_calls.append(k.get("comment") if "comment" in k else (a[2] if len(a) > 2 else None))
            return self.prog.pop(0) if len(self.prog) > 1 else self.prog[0]

        R.tt_progress = _prog

    def tearDown(self):
        R.tt_comment = self.orig_comment
        R.tt_progress = self.orig_prog
        R.tt_http_raw = self.orig_raw

    def _seed(self, round_no=1, status="running", session=None):
        R.save_runs({self.KEY: {"status": status, "issue_id": self.ISSUE,
                                "dispatch_id": 901, "session": session or "tt-edge-901",
                                "round": round_no}})
        return R.get_run(self.KEY)

    def test_r1_stale_suppress_rejects_running_replacement(self):
        """R1 — 409 응답 대기 중 다음 회차가 running으로 선점돼도 덮지 않는다.

        A의 finalize가 progress 응답을 기다리는 동안 B가 흡수·선점되어 실행 중일
        때, A의 늦은 409는 update_run_if_round 회차 가드로 거부된다(transition
        표만으로는 running→done이 합법이라 막히지 않음 — 회차 비교 필수)."""
        snap = self._seed(round_no=1)

        def prog_during_wait(*a, **k):
            # progress 네트워크 대기 중 다음 회차가 흡수·선점되어 running으로 교체됨
            self._seed(round_no=2, status="running", session="tt-edge-901-r2")
            return "stale"

        R.tt_progress = prog_during_wait
        outcome = R.finalize(None, self.KEY, snap, 0)
        self.assertEqual(outcome, "stale")
        ent = R.get_run(self.KEY)
        self.assertEqual(ent["round"], 2)
        self.assertEqual(ent["status"], "running")  # B 무훼손
        self.assertEqual(self.comments, [])

    def test_r3_combined_acceptance_retries_transient_failure(self):
        """리뷰 R3 후속 — 통합 접수 재시도: 1~2회 순단은 폴백 코멘트 없이 회복.
        서버가 잠깐 응답 실패해도 CAS 경유 접수가 이뤄진다(claim 창 제거)."""
        snap = self._seed()
        self.prog = [False, True]  # 1회 순단 → 2회차 성공
        outcome = R.finalize(None, self.KEY, snap, 0)
        self.assertEqual(outcome, "done")
        self.assertEqual(self.comments, [])  # 폴백 없음

    def test_r3_comment_combined_on_success_and_recheck_on_loss(self):
        """R3 — 투영 성공 시 별도 /comments 발행 없음(서버가 원자 접수);
        응답 유실(False)이지만 서버 기록 재확인되면 역시 발행 없음."""
        snap = self._seed()
        self.prog = [True]
        outcome = R.finalize(None, self.KEY, snap, 0)
        self.assertEqual(outcome, "done")
        self.assertEqual(self.comments, [])  # 통합 접수 — 이중 코멘트 없음
        # 응답 유실 + 서버 기록 확인 → 역시 발행 없음
        R.save_runs({self.KEY: dict(snap, status="running")})
        self.prog = [False]
        R.tt_http_raw = lambda m, p, payload=None: [  # GET dispatches 응답 모사
            {"id": 901, "run_state": "finished"}]
        outcome = R.finalize(None, self.KEY, R.get_run(self.KEY), 0)
        self.assertEqual(outcome, "done")
        self.assertEqual(self.comments, [])
        # 응답 유실 + 서버 미기록(진짜 다운) → CAS 우회 폴백 제거(R3): 코멘트 발행
        # 없이 pending 보관, 회복 후 watch_once가 progress 경유 재전송
        R.save_runs({self.KEY: dict(snap, status="running")})
        self.prog = [False]
        R.tt_http_raw = lambda m, p, payload=None: {"HTTP": 0, "detail": "conn refused"}
        outcome = R.finalize(None, self.KEY, R.get_run(self.KEY), 0)
        self.assertEqual(outcome, "done")
        self.assertEqual(self.comments, [])  # /comments 폴백 없음 — CAS 우회 차단
        self.assertTrue(os.path.exists(R._pending_path(self.KEY)))
        # 서버 회복 — CAS 경유 재전송 성공, 파일 정리 (코멘트 경로 아님)
        self.prog = [True]
        R._flush_pending_reports()
        self.assertFalse(os.path.exists(R._pending_path(self.KEY)))
        self.assertEqual(self.comments, [])

    def test_r7_observation_failure_discards_cross_round_silence(self):
        """리뷰 3차 R7 — 관찰 실패 분기도 회차 가드: 이전 회차 관찰자의 늦은 쓰기
        (동일 지문) 뒤 새 회차 첫 캡처가 실패하면 이전 무음을 승계하지 않는다."""
        now = time.time()
        R.save_runs({"R7B#902": {"status": "running", "issue_id": "R7B",
                                 "dispatch_id": 902, "session": "tt-x-902", "round": 2}})
        orig_fp = R.pane_fingerprint
        try:
            # 이전 회차 관찰자의 늦은 쓰기 모사 — round 1 + 오래된 pane_ts + 동일 지문
            R.write_heartbeat("R7B#902", {"ts": now, "round": 1, "ok": True,
                                          "dispatch_id": 902, "session": "tt-x-902",
                                          "pane_fp": "fp-x", "pane_ts": now - 900})
            R.pane_fingerprint = lambda name, key=None: None  # 새 회차 첫 관찰 실패
            R.heartbeat_pass()
            hb = R.read_heartbeat("R7B#902")
            self.assertIsNone(hb["pane_fp"])  # 회차 불일치 — 지문 폐기
            self.assertIsNone(hb["pane_ts"])
            # 다음 관찰(성공, 동일 지문) — 무음 기준 새로 시작(오래된 pane_ts 승계 없음)
            R.pane_fingerprint = lambda name, key=None: "fp-x"
            R.heartbeat_pass()
            hb2 = R.read_heartbeat("R7B#902")
            self.assertGreater(hb2["pane_ts"], now - 600)
        finally:
            R.pane_fingerprint = orig_fp

    def test_r6_capture_failure_never_kills(self):
        """R6 — 관찰 실패(ok=False) 순회는 과거 지문이 임계를 넘어도 판정 스킵."""
        now = time.time()
        self._seed()
        # 유효 관찰(오래된 pane_ts) 직후 관찰 실패 — 과거 지문은 보존되지만 ok=False
        R.write_heartbeat(self.KEY, {"ts": now, "round": 1, "ok": True,
                                     "dispatch_id": 901, "session": "tt-edge-901",
                                     "pane_fp": "old", "pane_ts": now - R.STALL_SILENCE_S * 3})
        R.write_heartbeat(self.KEY, {"ts": now, "round": 1, "ok": False,
                                     "dispatch_id": 901, "session": "tt-edge-901",
                                     "pane_fp": "old", "pane_ts": now - R.STALL_SILENCE_S * 3})
        killed = []
        R.tmux_run = lambda *a, **k: killed.append(a) or subprocess.CompletedProcess(a, 0)
        self.assertFalse(R.stall_check(self.KEY, R.get_run(self.KEY), now))
        self.assertEqual(killed, [])  # 관찰 없는 kill 없음
        self.assertEqual(R.get_run(self.KEY)["status"], "running")

    def test_r6_heartbeat_pass_marks_observation_failure(self):
        """R6 — pane_fingerprint 실패(None) 시 ok=False 기록, 유효 관찰 복원 시 갱신."""
        self._seed()
        orig_fp = R.pane_fingerprint
        try:
            R.pane_fingerprint = lambda name, key=None: None
            R.heartbeat_pass()
            hb = R.read_heartbeat(self.KEY)
            self.assertFalse(hb["ok"])
            # 관찰 복원 — ok=True + 새 pane_ts
            R.pane_fingerprint = lambda name, key=None: "fp-new"
            R.heartbeat_pass()
            hb2 = R.read_heartbeat(self.KEY)
            self.assertTrue(hb2["ok"])
            self.assertEqual(hb2["pane_fp"], "fp-new")
        finally:
            R.pane_fingerprint = orig_fp


if __name__ == "__main__":
    unittest.main()
