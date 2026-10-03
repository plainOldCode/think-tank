"""tt probe — 자율 스케줄러 (원 dispatchd). decide()는 판정 순수 함수.

내장 모드: 서버가 TT_PROBE_INTERVAL>0일 때 데몬 스레드로 loop() 구동 (M3ZW8E8A-ZK3G).
standalone 모드: dispatchd.py shim → main() (launchd 전환 기간 하위호환).
"""
from .core import (  # noqa: F401
    PULL_HINT, REPO, REPO_CARDS, api, card_from_branch, card_repo, ci_passed,
    collect_prs, collect_repos, decide, execute, gh_exec, gh_json, get_version,
    loop, main, run_once, snapshot,
)
