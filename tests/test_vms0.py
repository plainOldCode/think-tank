"""VMS0(M53PBEWX-VMS0) — 기계 댓글 서버 측 분리와 감소 측정.

WY6G는 CLI 표시만 바꿨다. VMS0는 ① 러너의 반복 상태 댓글(시작/계속)을 progress
투영으로 이미 기록된 dispatches 레코드에 맡기고 카드 타임라인에서 뺀다(종단 상태는
유지) ② 측정 스크립트로 전후 비교 창구를 만든다.
"""
import importlib.util as _ilu
import json
import os
import tempfile
import unittest
from unittest.mock import patch

# --- 러너 모듈 로드 (test_tt_runner.py와 같은 방식) ---
TMP = tempfile.mkdtemp(prefix="tt-vms0.")
os.environ["TT_RUNNER_STATE"] = os.path.join(TMP, "state")
os.environ["TT_URL"] = "http://127.0.0.1:1"
_spec = _ilu.spec_from_file_location(
    "tt_runner_vms0", os.path.join(os.path.dirname(__file__), "..", "runner", "tt-runner.py"))
R = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(R)


class StatusCommentTest(unittest.TestCase):
    def test_정기_상태_댓글은_기본_무음이다(self):
        with patch.object(R, "tt_comment") as tc:
            R._status_comment("X", "runner:m dispatch#1 → tmux s (queued, ws=/w)")
            tc.assert_not_called()

    def test_옵트인하면_카드에_쓴다(self):
        with patch.dict(os.environ, {"TT_RUNNER_STATUS_COMMENTS": "1"}):
            with patch.object(R, "tt_comment") as tc:
                R._status_comment("X", "hello")
                tc.assert_called_once()

    def test_시작_경로는_status_comment를_쓴다(self):
        """시작/계속 경로가 tt_comment를 직접 쓰지 않는다 — 소스 정적 검사."""
        src = open(os.path.join(os.path.dirname(__file__), "..", "runner", "tt-runner.py")).read()
        self.assertNotIn('tt_comment(ent["issue_id"], "runner:%s dispatch#%s → tmux', src)


class RatioTest(unittest.TestCase):
    def test_기계_판정은_cli_jq와_같다(self):
        from scripts.machine_comment_ratio import is_machine
        for a in ("probe", "board", "runner@m2max", "tt-server", "tt-embedding-daemon"):
            self.assertTrue(is_machine(a), a)
        for a in ("agy", "codex", "skshim", "kanban:x", "tester"):
            self.assertFalse(is_machine(a), a)

    def test_집계는_카드별_수와_비율을_낸다(self):
        from scripts.machine_comment_ratio import summarize
        issues = [
            {"id": "A", "comments": [{"author": "probe"}, {"author": "agy"},
                                     {"author": "runner@m2max"}, {"author": "probe"}]},
            {"id": "B", "comments": [{"author": "agy"}]},
            {"id": "C", "comments": []},
        ]
        s = summarize(issues)
        self.assertEqual(s["comments"], 5)
        self.assertEqual(s["machine"], 3)
        self.assertEqual(s["machine_ratio"], 0.6)
        self.assertEqual(s["worst"][0]["id"], "A")
        self.assertEqual(s["worst"][0]["machine"], 3)
        self.assertEqual(s["cards"], 3)


if __name__ == "__main__":
    unittest.main()
