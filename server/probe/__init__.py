"""tt probe — 자율 스케줄러. decide()는 판정 순수 함수.

서버가 TT_PROBE_INTERVAL>0일 때 데몬 스레드로 loop() 구동 (M3ZW8E8A-ZK3G).
standalone dispatchd는 U6(M5A4HZQB-AT3N)으로 퇴역 — shim·plist·TT_DISPATCH_* 제거.
"""
from .core import (  # noqa: F401
    REPO, REPO_CARDS, api, card_from_branch, card_repo, ci_passed,
    collect_prs, collect_repos, decide, execute, gh_exec, gh_json, get_version,
    hydrate_reviews, loop, review_verdict, run_once, snapshot,
)
