#!/usr/bin/env bash
# secret-scan.sh — push 전 tracked 파일에서 민감 패턴을 찾는다. 히트 시 exit 1.
# 사용: scripts/secret-scan.sh [repo-root]   (tt push가 자동 호출)
# 개인·환경 고유 패턴은 gitignored scripts/secret-patterns.local 에 한 줄씩.
set -uo pipefail
cd "${1:-.}"
PATTERNS=(
  '100\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}'   # tailscale/100net 주소
  '/Users/(?!YOU(/|$))[A-Za-z0-9._-]+'        # 실제 홈 경로 (YOU placeholder 허용)
  '/home/(?!YOU(/|$))[a-z0-9._-]+'
  'ghp_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{20,}'
)
if [[ -f scripts/secret-patterns.local ]]; then
  while IFS= read -r line; do [[ -n "$line" && ! "$line" == \#* ]] && PATTERNS+=("$line"); done < scripts/secret-patterns.local
fi
hits=0
for pat in "${PATTERNS[@]}"; do
  out="$(git grep -I -P -n "$pat" -- ':!.venv' ':!scripts/secret-scan.sh' ':!scripts/secret-patterns.local' 2>/dev/null)"
  if [[ -n "$out" ]]; then
    echo "✗ 패턴 [$pat]:"
    echo "$out" | head -10
    hits=1
  fi
done
[[ $hits -eq 1 ]] && { echo "secret-scan 실패 — 위 항목 스럽 후 재시도"; exit 1; }
echo "secret-scan 통과"
