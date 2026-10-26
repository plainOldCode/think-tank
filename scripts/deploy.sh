#!/usr/bin/env bash
# Tell an operator how to start turbo. This script does not deploy, tag,
# curl a server, or copy files.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
echo "turbo is the board. This script does not deploy."
echo "start: TURBO_DB=data/turbo.db TURBO_PORT=7810 python3 -m turbo"
echo "migrate: python3 -m turbo.migrate --legacy <legacy.db> --turbo data/turbo.db"
echo "dry-run: python3 -m turbo.migrate --legacy <legacy.db> --turbo data/turbo.db --dry-run"
exit 0
