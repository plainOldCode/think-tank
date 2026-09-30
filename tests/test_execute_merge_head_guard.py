"""probe merge 판정-집행 경합 회귀 — TT M3PPRJ49-FYW2 / Kanban t_501e6ec3.

실 GitHub 실측(2026-09-29) 기준으로 dispatchd의 probe merge 경로를 고정한다:
  1. gh pr view 단일 필드도 JSON 객체('{"headRefOid":"<sha>"}')로 온다 —
     파싱은 객체에서 필드를 꺼내야 한다(문자열 strip('"')면 영구 불일치).
  2. 병합 직전 head SHA 재확인: collect 시점 head_sha와 다르면 merge 폐기
     (판정-집행 경합 흡수 — probe 카드의 merge 직전 재확인 요구 동일 준수).
  3. gh pr list --head 는 exact prefix(실측 'tt/' → [] 0건). 목록은 전체 조회 +
     decide의 브랜치 역참조로만 후보를 좁힌다(collect_prs 회귀).
  4. gh pr view 는 번호를 '위치 인자'로 받는다(--number 플래그 없음 — gh가
     'unknown flag' 또는 타입 오류로 실패한다). FakeGh가 이 문법을 강제한다.

pytest 없는 환경 대응: stdlib unittest + gh/exec 주입(실 dispatchd 임포트).
실행: python3 -m unittest tests.test_execute_merge_head_guard
"""
import importlib.util
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

_spec = importlib.util.spec_from_file_location(
    "dispatchd", ROOT / "server" / "dispatchd.py")
assert _spec is not None and _spec.loader is not None
dispatchd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dispatchd)


class FakeGh:
    """실 gh CLI 문법/응답 형태 모사(실측 2026-09-29):
    - pr view <num> --repo R --json f : 번호 위치 인자, 응답은 필드 객체.
    - pr list/list --head : --head 는 exact prefix만(--head 'tt/' → []).
    - pr merge : 성공 시 문자열.
    """

    def __init__(self, *, head_at_view, merge_ok=True, view_raises=False):
        self.head_at_view = head_at_view
        self.merge_ok = merge_ok
        self.view_raises = view_raises
        self.calls = []

    def __call__(self, *args):
        args = [str(a) for a in args]
        self.calls.append(args)
        top, sub = args[0], args[1]
        if top == "pr" and sub == "view":
            if "--number" in args:
                raise RuntimeError("gh pr view: unknown flag: --number")
            if self.view_raises:
                raise RuntimeError("gh pr view: HTTP 401")
            # gh_json 경계약: JSON 문자열(실 gh_json이 loads 통과)
            return json.dumps({"headRefOid": self.head_at_view})
        if top == "pr" and sub == "merge":
            if not self.merge_ok:
                raise RuntimeError("HTTP 405: Not mergeable")
            return "Merged"
        if top == "pr" and sub == "list":
            if "--head" in args:  # 실측: exact prefix 필터
                raise AssertionError(
                    "pr list에 --head 접두어 사용 금지(정확히 일치만 매칭 → 0건)")
            if args[args.index("--json") + 1] == "number,headRefName,headRefOid":
                return json.dumps([{
                    "number": 6,
                    "headRefName": "tt/M3PPRJ49-FYW2-ci",
                    "headRefOid": self.head_at_view}])
        raise AssertionError(f"unexpected gh args: {args}")


def gh_json_like(raw_gh):
    """실 gh_json 계약(문자열 → parsed)을 FakeGh(문자열 반환) 위에 얹는다."""
    def gh_json(*args):
        text = raw_gh(*args)
        return json.loads(text) if text else None
    return gh_json


class FakeApi:
    """dispatchd.api 주입: TT 카드 조회/PATCH/코멘트 기록."""

    def __init__(self, issue):
        self.issue = issue
        self.patched = []
        self.commented = []

    def __call__(self, url, path, method="GET", body=None):
        if method == "GET":
            return dict(self.issue)
        if method == "PATCH":
            self.patched.append(body)
            return dict(self.issue)
        if method == "POST":
            self.commented.append(body)
            return {"ok": True}
        raise AssertionError(f"unexpected {method} {path}")


def merge_action(head_sha="a" * 40):
    return {"agent": "probe", "issue": "M3PPRJ49-FYW2", "action": "merge",
            "pr": 6, "head_sha": head_sha}


def issue_card():
    return {"id": "M3PPRJ49-FYW2", "state": "done", "version": 9,
            "completion_report": {"result": "passed"}}


class HeadRaceGuard(unittest.TestCase):

    def setUp(self):
        self.backups = (dispatchd.api, dispatchd.gh_exec, dispatchd.gh_json)

    def tearDown(self):
        dispatchd.api, dispatchd.gh_exec, dispatchd.gh_json = self.backups

    def run_execute(self, gh):
        calls = []

        def gh_exec(*a):
            calls.append(list(map(str, a)))
            return gh(*a)

        dispatchd.gh_exec = gh_exec
        dispatchd.gh_json = gh_json_like(gh)
        api = FakeApi(issue_card())
        dispatchd.api = api
        dispatchd.execute("http://x", merge_action())
        return calls, api

    def test_head_changed_skips_merge(self):
        """판정 후 head가 바뀌면 병합하면 안 된다(경합 흡수)."""
        gh = FakeGh(head_at_view="b" * 40)
        calls, _ = self.run_execute(gh)
        self.assertEqual([c for c in calls if c[:2] == ["pr", "merge"]], [],
                         "head 변경 시 merge 호출 금지")

    def test_head_unchanged_merges(self):
        gh = FakeGh(head_at_view="a" * 40)
        calls, api = self.run_execute(gh)
        merges = [c for c in calls if c[:2] == ["pr", "merge"]]
        self.assertEqual(len(merges), 1, "판정 두면 병합 1회")
        self.assertIn("--squash", merges[0])

    def test_view_uses_positional_and_json_object(self):
        """pr view 는 위치 인자 + 객체 응답 파싱(실 gh 문법/형태)."""
        gh = FakeGh(head_at_view="a" * 40)
        calls, _ = self.run_execute(gh)
        # pr view는 gh_json 경로(FakeGh.calls), merge는 gh_exec 경로(calls)
        views = [c for c in gh.calls if c[:2] == ["pr", "view"]]
        self.assertEqual(len(views), 1, "재확인 pr view 1회")
        self.assertEqual(views[0][:3], ["pr", "view", "6"],
                         f"번호는 위치 인자: {views[0]}")

    def test_view_failure_skips_merge(self):
        """재확인 조회 실패(401 등)는 안전 방향: 병합하지 않는다."""
        gh = FakeGh(head_at_view="a" * 40, view_raises=True)
        calls, _ = self.run_execute(gh)
        self.assertEqual([c for c in calls if c[:2] == ["pr", "merge"]], [])


class CollectPrsGrammar(unittest.TestCase):

    def test_collect_prs_no_head_prefix_flag(self):
        """collect_prs는 --head를 쓰면 안 된다(실측 'tt/' → 0건 = probe 실명).
        pr checks 조회 실패는 체크 [] 격리(개 PR은 계속, ci_passed가 차단)."""
        gh = FakeGh(head_at_view="a" * 40)
        json_backup = dispatchd.gh_json
        dispatchd.gh_json = gh_json_like(gh)
        try:
            out = dispatchd.collect_prs()
        finally:
            dispatchd.gh_json = json_backup
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["branch"], "tt/M3PPRJ49-FYW2-ci")
        self.assertEqual(out[0]["head_sha"], "a" * 40)
        self.assertEqual(out[0]["checks"], [], "checks 조회 실패는 [] 격리")


class CiPassedLiveShape(unittest.TestCase):

    def test_pr_checks_states_are_uppercase(self):
        """실측: gh pr checks --json state 는 대문자(SUCCESS/FAILURE/PENDING).
        ci_passed는 이 형태 기준으로 판정(소문자 체크러닝 오인 없음)."""
        green = {"checks": [{"name": "pytest", "state": "SUCCESS"},
                            {"name": "smoke-node", "state": "SUCCESS"},
                            {"name": "secret-scan", "state": "SUCCESS"}]}
        self.assertTrue(dispatchd.ci_passed(green))
        # 실 PR#7(고의 실행 데모) 형태: tests=failure → 차단
        red = {"checks": [{"name": "tests", "state": "FAILURE"}]}
        self.assertFalse(dispatchd.ci_passed(red))
        # 실 PR#3 형태: no checks reported → [] → 차단(결함 없는 안전 방향)
        self.assertFalse(dispatchd.ci_passed({"checks": []}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
