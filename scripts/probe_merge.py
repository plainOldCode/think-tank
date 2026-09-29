#!/usr/bin/env python3
"""tt-probe-merge — green-PR 안전 자동 병합 순회 감시자.

TT M3PPRJBV-B52W / Kanban t_019a9738. dispatchd와 동일 계층: decide()는 순수 함수,
run_round()는 주입된 gh/tt 경계만 소비. stdlib + gh CLI만 사용, 서브프로세스·서버 수정 없음.

green-PR 정의(요구사항 확정서 §3·§4):
  OPEN + 비draft + mergeable=MERGEABLE + mergeStateStatus=CLEAN
  + REQUIRED_CHECKS 전부가 PR 현재 head SHA에서 conclusion=SUCCESS
  + statusCheckRollup 비어있지 않음(빈 rollup=차단 — CI 부재 PR 오인 방지).
추가 게이트(카드 연결): head 브랜치 tt/<카드ID>-slug → TT 카드 state=done
  + completion_report.result=passed. 판정 불가를 항상 스킵(안전 방향)으로 취급.
브랜치 보호 우회 금지: --admin/--force/--no-verify 없음. merge 직전 head SHA 재확인
로 판정-실행 경합을 흡수하고, 실패 시 다음 라운드에 자연 재시도(중복 병합 없음).
probe는 PR만 병합한다 — TT/Kanban 카드 전이는 워커·브리지 몫(쓰기 0).

env: GH_OWNER GH_REPO REQUIRED_CHECKS TT_URL TT_AGENT DRYRUN=1 KILL=1
launchd/cron 단일 인스턴스 전제 + lockfile(pid)로 2중 실행 차단.
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

GH_OWNER = os.environ.get("GH_OWNER", "plainOldCode")
GH_REPO = os.environ.get("GH_REPO", "think-tank")
TT_URL = os.environ.get("TT_URL", "http://skshim-mini.tail8a267b.ts.net:7800")
TT_AGENT = os.environ.get("TT_AGENT", "probe@unknown")
REQUIRED_CHECKS = tuple(
    c.strip() for c in os.environ.get(
        "REQUIRED_CHECKS", "pytest,smoke-node,secret-scan").split(",") if c.strip())
DRYRUN = os.environ.get("DRYRUN") == "1"
KILL = os.environ.get("KILL") == "1"
LOCK_PATH = os.environ.get("PROBE_LOCK",
                           os.path.expanduser("~/.local/state/tt-probe-merge.lock"))
MAX_BACKOFF = 300  # dispatchd 동일 철학: 장애 시 지수 백오프 상한(초)

CARD_BRANCH = re.compile(r"^tt/([A-Z0-9]+-[A-Z0-9]+)(?:-|$)")
PR_FIELDS = ("number,state,headRefName,headRefOid,isDraft,mergeable,"
             "mergeStateStatus,statusCheckRollup")


class GhError(Exception):
    def __init__(self, args, code, stderr=""):
        super().__init__(f"gh {' '.join(args)} -> exit {code}: {stderr[:200]}")
        self.args_ = args
        self.code = code
        self.stderr = stderr


class TtError(Exception):
    pass


class ProbeLocked(Exception):
    pass


# --- 순수 판정 --------------------------------------------------------------

def parse_card_id(branch):
    m = CARD_BRANCH.match(branch or "")
    return m.group(1) if m else None


def decide(pr, check_runs, head_recheck, tt, required=REQUIRED_CHECKS):
    """green-PR + 카드 연결 판정. decision: merge | skip(+reason). 순수."""
    head = pr.get("headRefOid", "")
    out = {"decision": "skip", "reason": "", "head_sha": head, "pr": pr.get("number")}

    def skip(reason):
        out["reason"] = reason
        return out

    if pr.get("state") not in (None, "OPEN"):
        return skip(f"not-open:{pr.get('state')}")
    if pr.get("isDraft"):
        return skip("draft")
    if pr.get("mergeable") not in (None, "MERGEABLE"):
        return skip(f"not-mergeable:{pr.get('mergeable')}")
    if pr.get("mergeStateStatus") not in (None, "CLEAN"):
        return skip(f"not-clean:{pr.get('mergeStateStatus')}")

    card_id = parse_card_id(pr.get("headRefName"))
    if not card_id:
        return skip("no-card-branch")
    if tt is None:
        return skip("tt-unreachable")
    if tt.get("state") != "done":
        return skip("tt-not-done")
    report = tt.get("completion_report")
    if not report:
        return skip("tt-no-report")
    if report.get("result") != "passed":
        return skip("tt-report-not-passed")

    rollup = pr.get("statusCheckRollup")
    if rollup is not None and len(rollup) == 0:
        return skip("empty-rollup")

    by_name = {}
    for run in check_runs:
        by_name.setdefault(run.get("name"), []).append(run)
    for name in required:
        runs = by_name.get(name)
        if not runs:
            return skip(f"check-missing:{name}")
        fresh = [r for r in runs if r.get("head_sha") == head]
        if not fresh:
            return skip(f"stale-check-run:{name}")
        r = fresh[0]
        if r.get("status") != "COMPLETED":
            return skip(f"check-not-completed:{name}")
        if r.get("conclusion") != "SUCCESS":
            return skip(f"check-failed:{name}:{r.get('conclusion')}")

    if head_recheck != head:
        return skip("head-changed")

    out["decision"] = "merge"
    out["reason"] = "green+card-done"
    out["card_id"] = card_id
    return out


def format_log(pr, decision):
    ts = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    return (f"{ts} PR:{pr.get('number')} {decision.get('head_sha', '')} "
            f"{decision.get('decision', '?')} {decision.get('reason', '')}")


# --- 경계 구현(실환경: gh CLI / TT HTTP) -------------------------------------

class GhCli:
    def run(self, args):
        proc = subprocess.run(["gh"] + [str(a) for a in args],
                              capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            raise GhError(args, proc.returncode, proc.stderr.strip())
        return proc.stdout.strip()


class TtClient:
    def __init__(self, base_url=TT_URL):
        self.base_url = base_url.rstrip("/")

    def get_issue(self, issue_id):
        req = urllib.request.Request(f"{self.base_url}/issues/{issue_id}",
                                     headers={"X-TT-Agent": TT_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.load(resp)
        except (urllib.error.URLError, urllib.error.HTTPError, OSError,
                json.JSONDecodeError) as e:
            raise TtError(f"TT GET /issues/{issue_id}: {e}") from e


class single_instance:
    """pid lockfile: 살아있으면 ProbeLocked, 죽은 잔존 lock은 회수."""

    def __init__(self, path=LOCK_PATH):
        self.path = str(path)
        self.fh = None

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        if os.path.exists(self.path):
            try:
                with open(self.path) as f:
                    pid = int(f.read().strip())
                os.kill(pid, 0)
                raise ProbeLocked(f"probe already running pid={pid}")
            except (ValueError, ProcessLookupError, PermissionError):
                pass  # 회수: 잔존 lock이나 소유 프로세스 없음(PermissionError=요정)
        self.fh = open(self.path, "w")
        self.fh.write(str(os.getpid()))
        self.fh.flush()
        return self

    def __exit__(self, *exc):
        try:
            os.unlink(self.path)
        except OSError:
            pass
        if self.fh:
            self.fh.close()
        return False


# --- 라운드 실행기(주입식: gh.run / tt.get_issue) -----------------------------

def run_round(gh, tt, required=REQUIRED_CHECKS, dryrun=DRYRUN,
              owner=GH_OWNER, repo=GH_REPO, log=print, kill=None):
    """1순회: 후보 수집 → 판정 → head 재확인 → squash 병합. 반환 요약 dict."""
    result = {"merged": [], "would_merge": [], "skipped": [], "aborted": False,
              "killed": False}
    if kill is None:
        kill = lambda: KILL  # noqa: E731 (env 토글 기본)
    if kill():
        result["killed"] = True
        log(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} ROUND killed — KILL 토글, 쓰기 0")
        return result

    try:
        listed = json.loads(gh.run(
            ["pr", "list", "--repo", f"{owner}/{repo}", "--state", "open",
             "--head", "tt/", "--json", PR_FIELDS]))
    except (GhError, json.JSONDecodeError) as e:
        result["aborted"] = True
        log(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} ROUND aborted — 목록 조회 실패: {e}")
        return result

    for pr in listed:
        if kill():
            result["killed"] = True
            log(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} ROUND killed 중도 — 이후 후보 미처리")
            return result
        num = pr.get("number")
        head = pr.get("headRefOid", "")
        try:
            card_id = parse_card_id(pr.get("headRefName"))
            if card_id:
                path = f"repos/{owner}/{repo}/commits/{head}/check-runs"
                check_runs = json.loads(gh.run(["api", path])).get("check_runs", [])
                tt_issue = tt.get_issue(card_id)
            else:
                check_runs, tt_issue = [], None
        except TtError as e:
            log(format_log(pr, {"decision": "skip", "head_sha": head,
                                "reason": "tt-unreachable"}) + f" ({e})")
            result["skipped"].append((num, "tt-unreachable"))
            continue
        except (GhError, json.JSONDecodeError) as e:
            # 판정 근거 조회 실패 = 판정 불가 → 안전 skip(합병 금지), 라운드는 계속
            reason = "check-unreachable"
            log(format_log(pr, {"decision": "skip", "head_sha": head,
                                "reason": reason}) + f" ({e})")
            result["skipped"].append((num, reason))
            continue

        decision = decide(pr, check_runs, head, tt_issue, required)
        log(format_log(pr, decision))

        if decision["decision"] != "merge":
            result["skipped"].append((num, decision["reason"]))
            continue

        if dryrun:
            result["would_merge"].append(num)
            continue

        # 판정-실행 경합 흡수: 병합 직전 현재 head SHA 재확인, 다르면 폐기(다음 라운드)
        try:
            recheck = gh.run(["pr", "view", "--repo", f"{owner}/{repo}",
                              "--number", num, "--json", "headRefOid"]).strip()
            recheck = recheck.strip('"')
        except GhError as e:
            log(format_log(pr, {"decision": "skip", "head_sha": head,
                                "reason": f"recheck-error ({e})"}))
            result["skipped"].append((num, "recheck-error"))
            continue
        if recheck != head:
            log(format_log(pr, {"decision": "skip", "head_sha": head,
                                "reason": "head-changed-at-merge"}))
            result["skipped"].append((num, "head-changed"))
            continue

        try:
            gh.run(["pr", "merge", str(num), "--squash", "--delete-branch"])
        except GhError as e:
            # 405/non-mergeable 등은 라운드 중단 아닌 skip: 다음 라운드 자연 재시도
            log(format_log(pr, {"decision": "skip", "head_sha": head,
                                "reason": f"merge-error ({e})"}))
            result["skipped"].append((num, "merge-error"))
            continue
        result["merged"].append(num)
        log(format_log(pr, {"decision": "merge", "head_sha": head,
                            "reason": "merged"}))
    return result


def next_backoff(attempt):
    return min(60 * (2 ** (attempt - 1)), MAX_BACKOFF)


def main():
    attempt = 0
    while True:
        try:
            with single_instance():
                out = run_round(gh=GhCli(), tt=TtClient())
            if not out["aborted"]:
                merged = ",".join(str(n) for n in out["merged"]) or "-"
                skipped = ",".join(f"{n}:{r}" for n, r in out["skipped"]) or "-"
                print(f"ROUND ok merged={merged} would={out['would_merge']} skipped={skipped}")
                return 0
            raise RuntimeError("round aborted")
        except ProbeLocked as e:
            print(f"ROUND skipped — {e}")
            return 0
        except Exception as e:  # noqa: BLE001 — 관측 후 백오프(상태 오염 없음: 쓰기 0 라운드)
            attempt += 1
            delay = next_backoff(attempt)
            print(f"ROUND error attempt={attempt}: {e} — 백오프 {delay}s "
                  f"(KILL=1 또는 재실행으로 중단 가능)")
            if attempt >= 3 or KILL:
                return 2
            time.sleep(delay)


if __name__ == "__main__":
    sys.exit(main())
