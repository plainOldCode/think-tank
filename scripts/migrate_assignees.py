#!/usr/bin/env python3
"""DX88(M5283J3A-DX88) — 기존 DB assignee/lease_by 정규화 마이그레이션.

dry-run이 기본(변경 없이 계획 통계만 출력), --apply 시 실제 반영. 멱등 — 반복 실행 안전.
배포 순서: PR 병합 → 10-24 게이트 해제 → 실워커 별칭 등록 → 이 스크립트 --apply → 서버 재시작.
W09 실험 창 중에는 실행하지 않는다(지표 데이터 재작성 방지).

사용: python3 scripts/migrate_assignees.py [db_path] [--apply]
"""
import argparse
import json
import pathlib
import sqlite3
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "server"))

import migrations

default_db = pathlib.Path(__file__).resolve().parent.parent / "data" / "tt.db"
p = argparse.ArgumentParser(description="기존 assignee/lease_by 정규화 (DX88)")
p.add_argument("db", nargs="?", default=str(default_db), help="SQLite DB 경로")
p.add_argument("--apply", action="store_true", help="실제 반영(기본 dry-run)")
args = p.parse_args()

con = sqlite3.connect(args.db)
con.row_factory = sqlite3.Row
stats = migrations.migrate_assignees(con, apply=args.apply)
con.close()
print(json.dumps(stats))
