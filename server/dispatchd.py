#!/usr/bin/env python3
"""tt-dispatchd standalone 진입점 — 로직은 probe/ 패키지로 이관 (M3ZW8E8A-ZK3G).

launchd com.tt.dispatchd 전환 기간 동안 기존 실행 경로와 test import를 유지하는 shim.
완전 제거 후에는 서버 내장 스레드(TT_PROBE_INTERVAL)만 사용.
"""
from probe import *  # noqa: F401,F403 — public API 전체 re-export
from probe import main  # noqa: F401

if __name__ == "__main__":
    main()
