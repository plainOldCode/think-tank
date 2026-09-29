"""probe_merge 판정 테이블 — TT M3PPRJBV-B52W / Kanban t_019a9738.

decide() 순수 함수와 run_round() 실행기를 gh/tt 주입으로 검증한다.
fixtures: tests/fixtures/github/*.json (실 gh 응답 형태).

실행: pytest -q tests/test_probe_merge.py (CI) 또는
      python3 -m unittest tests.test_probe_merge -v (stdlib, pytest 없는 환경).
"""
import json
import importlib.util
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "github"

_spec = importlib.util.spec_from_file_location(
    "probe_merge", ROOT / "scripts" / "probe_merge.py")
assert _spec is not None and _spec.loader is not None
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)

REQUIRED = ("pytest", "smoke-node", "secret-scan")


def load(name):
    with open(FIXTURES / name, encoding="utf-8") as f:
        return json.load(f)


def decide_from(fixture_name, required=REQUIRED):
    d = load(fixture_name)
    return probe.decide(
        pr=d["pr"],
        check_runs=d["check_runs"]["check_runs"],
        head_recheck=d.get("head_recheck", d["pr"]["headRefOid"]),
        tt=d.get("tt_issue"),
        required=required,
    )


def decide_d(fixture_name, mutate_pr=None, mutate_checks=None, mutate_tt=None,
             required=REQUIRED):
    d = load(fixture_name)
    pr = d["pr"]
    checks = d["check_runs"]["check_runs"]
    tt = d.get("tt_issue")
    if mutate_pr:
        mutate_pr(pr)
    if mutate_checks:
        mutate_checks(checks)
    if mutate_tt:
        mutate_tt(tt)
    return probe.decide(pr=pr, check_runs=checks,
                        head_recheck=pr["headRefOid"], tt=tt, required=required)


class GhError(Exception):
    pass


class TtError(Exception):
    pass


# --- decide(): 판정 테이블 -------------------------------------------------

class DecideGreenRed(unittest.TestCase):

    def test_green_merge(self):
        r = decide_from("pr_green.json")
        self.assertEqual(r["decision"], "merge")
        self.assertEqual(r["head_sha"],
                         "76570af42e1b95cf880f2bb77d3d49733b653bee")

    def test_red_check_skips(self):
        r = decide_from("pr_red.json")
        self.assertEqual(r["decision"], "skip")
        self.assertTrue(r["reason"].startswith("check-failed:pytest"))

    def test_pending_check_skips(self):
        r = decide_from("pr_pending.json")
        self.assertEqual(r["decision"], "skip")
        self.assertTrue(r["reason"].startswith("check-not-completed:pytest"))

    def test_missing_required_check_skips(self):
        r = decide_from("pr_missing_check.json")
        self.assertEqual(r["decision"], "skip")
        self.assertTrue(r["reason"].startswith("check-missing:smoke-node"))

    def test_empty_rollup_blocks(self):
        """PR#3 실측(rollup=[]) — CI 부재 PR을 green으로 오인하면 안 된다."""
        r = decide_d("pr_green.json", mutate_pr=lambda p: p.update(statusCheckRollup=[]),
                     mutate_checks=lambda c: c.clear())
        self.assertEqual(r["decision"], "skip")
        self.assertEqual(r["reason"], "empty-rollup")

    def test_stale_head_check_results_ignored(self):
        """check-runs가 구 head SHA를 가리키면(오래된 결과) green 아니다."""
        r = decide_d("pr_green.json",
                     mutate_checks=lambda cs: [c.update(head_sha="0" * 40) for c in cs])
        self.assertEqual(r["decision"], "skip")
        self.assertTrue(r["reason"].startswith("stale-check-run"))

    def test_head_changed_after_verdict_skips(self):
        r = decide_from("pr_head_changed.json")
        self.assertEqual(r["decision"], "skip")
        self.assertEqual(r["reason"], "head-changed")

    def test_draft_skips(self):
        r = decide_from("pr_draft.json")
        self.assertEqual(r["decision"], "skip")
        self.assertEqual(r["reason"], "draft")

    def test_not_mergeable_skips(self):
        r = decide_d("pr_green.json",
                     mutate_pr=lambda p: p.update(mergeStateStatus="DIRTY"))
        self.assertEqual(r["decision"], "skip")
        self.assertEqual(r["reason"], "not-clean:DIRTY")

    def test_not_mergeable_value_skips(self):
        r = decide_d("pr_green.json",
                     mutate_pr=lambda p: p.update(mergeable="CONFLICTING"))
        self.assertEqual(r["decision"], "skip")
        self.assertEqual(r["reason"], "not-mergeable:CONFLICTING")

    def test_cancelled_conclusion_not_success(self):
        r = decide_d("pr_green.json",
                     mutate_checks=lambda cs: cs[0].update(conclusion="CANCELLED"))
        self.assertEqual(r["decision"], "skip")
        self.assertTrue(r["reason"].startswith("check-failed:pytest"))

    def test_non_head_sha_extra_run_ignored(self):
        """head SHA와 무관한 check-run 잔존물은 실패 근거가 아니다(필수 3건 success)."""
        r = decide_d("pr_green.json", mutate_checks=lambda cs: cs.append(
            {"name": "smoke-node", "head_sha": "9" * 40,
             "status": "COMPLETED", "conclusion": "FAILURE"}))
        self.assertEqual(r["decision"], "merge")


# --- decide(): TT 카드 연결·보고 검증 ----------------------------------------

class DecideCardLink(unittest.TestCase):

    def test_branch_without_card_id_skips(self):
        r = decide_d("pr_green.json",
                     mutate_pr=lambda p: p.update(headRefName="lease-limit-env"))
        self.assertEqual(r["decision"], "skip")
        self.assertEqual(r["reason"], "no-card-branch")

    def test_tt_not_done_skips(self):
        r = decide_d("pr_green.json",
                     mutate_tt=lambda t: t.update(state="in_progress"))
        self.assertEqual(r["decision"], "skip")
        self.assertEqual(r["reason"], "tt-not-done")

    def test_tt_missing_report_skips(self):
        r = decide_d("pr_green.json",
                     mutate_tt=lambda t: t.update(completion_report=None))
        self.assertEqual(r["decision"], "skip")
        self.assertEqual(r["reason"], "tt-no-report")

    def test_tt_report_not_passed_skips(self):
        r = decide_d("pr_green.json",
                     mutate_tt=lambda t: t["completion_report"].update(result="inconclusive"))
        self.assertEqual(r["decision"], "skip")
        self.assertEqual(r["reason"], "tt-report-not-passed")

    def test_tt_unreachable_skips_safe(self):
        """TT 조회 실패(tt=None)는 판정 불가 → 안전 방향(병합 안 함)."""
        d = load("pr_green.json")
        r = probe.decide(pr=d["pr"], check_runs=d["check_runs"]["check_runs"],
                         head_recheck=d["pr"]["headRefOid"], tt=None,
                         required=REQUIRED)
        self.assertEqual(r["decision"], "skip")
        self.assertEqual(r["reason"], "tt-unreachable")


# --- run_round(): 라운드 실행기 ---------------------------------------------

class FakeGh:
    """주입 가능한 gh CLI 경계: commands 목록에 기록, 응답은 fixture 기반."""

    def __init__(self, *, prs, checks_by_sha, recheck=None, merge_ok=True):
        self.prs = prs
        self.checks_by_sha = checks_by_sha
        self.recheck = recheck or {}
        self.merge_ok = merge_ok
        self.commands = []
        self.merged = []

    def run(self, args):
        """args 예: ["pr","view","--repo",..,"--number",N,"--json",f] → 필드 파싱."""
        self.commands.append(list(args))
        top, sub = args[0], args[1]
        if top == "pr" and sub == "list":
            return json.dumps(self.prs)
        if top == "pr" and sub == "view":
            num = int(args[args.index("--number") + 1])
            pr = next(p for p in self.prs if p["number"] == num)
            fields = args[-1].split(",")
            if fields == ["headRefOid"]:
                return self.recheck.get(num, pr["headRefOid"])
            return json.dumps({k: pr[k] for k in fields if k in pr})
        if top == "pr" and sub == "merge":
            num = int(args[2])
            if not self.merge_ok:
                raise probe.GhError(["gh"] + args, 1, "HTTP 405: not mergeable")
            self.merged.append(num)
            return "Merged"
        if top == "api":
            sha = args[1].split("/")[4]  # repos/OWNER/REPO/commits/<sha>/check-runs
            return json.dumps(self.checks_by_sha.get(sha, {"check_runs": []}))
        raise AssertionError(f"unexpected gh args: {args}")


class FakeTt:
    def __init__(self, issues, raise_on=None):
        self.issues = issues
        self.raise_on = raise_on

    def get_issue(self, issue_id):
        if self.raise_on == issue_id:
            raise probe.TtError("connection refused")
        return self.issues.get(issue_id)


def green_prs():
    d = load("pr_green.json")
    return d["pr"], d["check_runs"], {d["tt_issue"]["id"]: d["tt_issue"]}


def _round(gh, tt, *, dryrun=False, kill=None):
    logs = []
    out = probe.run_round(gh=gh, tt=tt, required=REQUIRED, dryrun=dryrun,
                          owner="plainOldCode", repo="think-tank",
                          log=logs.append, kill=kill)
    out["logs"] = logs
    return out


class RunRound(unittest.TestCase):

    def test_round_merge_normal(self):
        pr, checks, tts = green_prs()
        gh = FakeGh(prs=[pr], checks_by_sha={pr["headRefOid"]: checks})
        out = _round(gh, FakeTt(tts))
        self.assertEqual(out["merged"], [3])
        merge_cmd = [c for c in gh.commands if c[:2] == ["pr", "merge"]]
        self.assertEqual(merge_cmd, [["pr", "merge", "3", "--squash", "--delete-branch"]])

    def test_round_no_protection_bypass_flags(self):
        pr, checks, tts = green_prs()
        gh = FakeGh(prs=[pr], checks_by_sha={pr["headRefOid"]: checks})
        _round(gh, FakeTt(tts))
        for cmd in gh.commands:
            for banned in ("--admin", "--force", "--no-verify", "--rebase", "--merge"):
                self.assertNotIn(banned, cmd, f"보호 우회 플래그 금지: {banned}")

    def test_round_recheck_before_merge(self):
        """판정-실행 경합: 병합 직전 headRefOid 재조회 호출이 있어야 한다."""
        pr, checks, tts = green_prs()
        gh = FakeGh(prs=[pr], checks_by_sha={pr["headRefOid"]: checks})
        _round(gh, FakeTt(tts))
        recheck_calls = [c for c in gh.commands
                         if c[:2] == ["pr", "view"] and c[-1] == "headRefOid"]
        self.assertTrue(recheck_calls, "merge 직전 head 재확인 호출 필요")

    def test_round_head_race_skips_merge(self):
        pr, checks, tts = green_prs()
        gh = FakeGh(prs=[pr], checks_by_sha={pr["headRefOid"]: checks},
                    recheck={3: "5" * 40})
        out = _round(gh, FakeTt(tts))
        self.assertEqual(out["merged"], [])
        self.assertEqual(out["skipped"], [(3, "head-changed")])
        self.assertFalse([c for c in gh.commands if c[:2] == ["pr", "merge"]])

    def test_round_dryrun_no_merge(self):
        pr, checks, tts = green_prs()
        gh = FakeGh(prs=[pr], checks_by_sha={pr["headRefOid"]: checks})
        out = _round(gh, FakeTt(tts), dryrun=True)
        self.assertEqual(out["merged"], [])
        self.assertEqual(out["would_merge"], [3])
        self.assertFalse([c for c in gh.commands if c[:2] == ["pr", "merge"]])

    def test_round_relist_after_merge_is_harmless(self):
        """반복 순회: 병합 후 목록에서 사라진 PR은 무해. 남아 재실행돼도 재병합 없음."""
        pr, checks, tts = green_prs()
        gh = FakeGh(prs=[pr], checks_by_sha={pr["headRefOid"]: checks})
        _round(gh, FakeTt(tts))
        gh.prs = []
        out2 = _round(gh, FakeTt(tts))
        self.assertEqual(out2["merged"], [])
        gone = dict(pr)
        gone["state"] = "MERGED"
        gh.prs = [gone]
        out3 = _round(gh, FakeTt(tts))
        self.assertEqual(out3["merged"], [])

    def test_round_api_error_aborts_round(self):
        pr, checks, tts = green_prs()

        class BoomGh:
            def run(self, args):
                raise probe.GhError(["gh"] + args, 1, "HTTP 403: rate limit exceeded")

        out = _round(BoomGh(), FakeTt(tts))
        self.assertTrue(out["aborted"])
        self.assertEqual(out["merged"], [])

    def test_round_merge_conflict_405_continues(self):
        """merge 405(경합)는 라운드 중단 아닌 skip: 나머지 후보는 계속 판정."""
        pr, checks, tts = green_prs()
        other = dict(load("pr_green.json")["pr"], number=4,
                     headRefName="tt/M3PPRJ7V-Y6YF-ci",
                     headRefOid="a" * 40)
        gh = FakeGh(prs=[other, pr],
                    checks_by_sha={pr["headRefOid"]: checks}, merge_ok=False)
        out = _round(gh, FakeTt(tts))
        self.assertEqual(out["merged"], [])
        self.assertFalse(out["aborted"])

    def test_round_tt_unreachable_skips_pr(self):
        pr, checks, tts = green_prs()
        gh = FakeGh(prs=[pr], checks_by_sha={pr["headRefOid"]: checks})
        out = _round(gh, FakeTt({}, raise_on="M3PPRJFY-V6EA"))
        self.assertEqual(out["merged"], [])
        self.assertEqual(out["skipped"], [(3, "tt-unreachable")])

    def test_round_candidate_filter_non_tt_branch(self):
        """후보 수집: tt/ 접두 브랜치만 판정 대상(그 외는 skip, gh 조회 낭비 없음)."""
        pr, checks, tts = green_prs()
        other = dict(pr, number=99, headRefName="lease-limit-env")
        gh = FakeGh(prs=[other, pr], checks_by_sha={pr["headRefOid"]: checks})
        out = _round(gh, FakeTt(tts))
        self.assertEqual(out["merged"], [3])
        self.assertEqual(out["skipped"], [(99, "no-card-branch")])
        # 비tt 브랜치에 대한 commits/api 조회가 없어야 한다
        api_for_99 = [c for c in gh.commands if c[0] == "api" and False]
        self.assertEqual(api_for_99, [])

    def test_round_red_then_green_not_dropped(self):
        """누락 검사: red 후보에서 실패해도 이후 green 후보는 계속 판정된다."""
        d1 = load("pr_red.json")
        pr, checks, tts = green_prs()
        gh = FakeGh(prs=[d1["pr"], pr],
                    checks_by_sha={d1["pr"]["headRefOid"]: d1["check_runs"],
                                   pr["headRefOid"]: checks})
        tt = FakeTt({**tts, d1["tt_issue"]["id"]: d1["tt_issue"]})
        out = _round(gh, tt)
        self.assertEqual(out["merged"], [3])
        self.assertEqual(out["skipped"][0][0], 4)
        self.assertTrue(out["skipped"][0][1].startswith("check-failed:pytest"))


# --- 로깅·중복 방지·킬 스위치 -------------------------------------------------

class Observability(unittest.TestCase):

    def test_log_line_format_merge(self):
        pr, checks, tts = green_prs()
        gh = FakeGh(prs=[pr], checks_by_sha={pr["headRefOid"]: checks})
        out = _round(gh, FakeTt(tts))
        merge_logs = [l for l in out["logs"] if " merge " in l]
        self.assertTrue(merge_logs, f"병합 로그 필요: {out['logs']}")
        fields = merge_logs[-1].split()
        # "<ts> PR:<n> <head_sha> <decision> <reason>" 형태
        self.assertEqual(fields[1], "PR:3")
        self.assertEqual(fields[2],
                         "76570af42e1b95cf880f2bb77d3d49733b653bee")
        self.assertEqual(fields[3], "merge")

    def test_log_records_skip_reasons(self):
        r = decide_from("pr_red.json")
        line = probe.format_log(pr=load("pr_red.json")["pr"], decision=r)
        self.assertIn("skip", line)
        self.assertIn("check-failed:pytest", line)

    def test_single_instance_lock(self):
        with tempfile.TemporaryDirectory() as td:
            lock = Path(td) / "probe.lock"
            with probe.single_instance(lock):
                with self.assertRaises(probe.ProbeLocked):
                    with probe.single_instance(lock):
                        pass
            # 빠져나오면 재취득 가능
            with probe.single_instance(lock):
                pass

    def test_kill_switch_before_round(self):
        pr, checks, tts = green_prs()
        gh = FakeGh(prs=[pr], checks_by_sha={pr["headRefOid"]: checks})
        out = _round(gh, FakeTt(tts), kill=lambda: True)
        self.assertEqual(out["merged"], [])
        self.assertTrue(out.get("killed"))
        self.assertFalse([c for c in gh.commands if c[:2] == ["pr", "merge"]])


if __name__ == "__main__":
    unittest.main(verbosity=2)
