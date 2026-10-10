#!/usr/bin/env bash
# stale-base-check.sh — PR 생성 전 브랜치 base가 main보다 뒤인지 검사한다.
# 사용: scripts/stale-base-check.sh [repo-root] [base-ref]
#   (tt push는 TT_STALE_WARN=1로 호출해 경고만 하고 통과시킨다)
# 기수락 없음: 뒤처진 base로 만든 PR은 리뷰·머지에서 CONFLICTING으로 전멸했다
# (PR #53~58 사고 — 계약 제5조 '반복 실수는 구조로 막는다'). 히트 시 exit 1.
set -uo pipefail
cd "${1:-.}"
BASE="${2:-origin/main}"
WARN="${TT_STALE_WARN:-0}"

# base 해석: 로컬에 없으면 원격에서 가져온다 (네트워크 실패 시 기존 ref로 판단)
if ! git rev-parse --verify --quiet "$BASE" >/dev/null; then
  git fetch -q --no-tags "${BASE%%/*}" 2>/dev/null || true
fi
if ! git rev-parse --verify --quiet "$BASE" >/dev/null; then
  echo "stale-base: base ref '$BASE' 없음 — 검사 생략" >&2
  exit 0
fi
HEAD_REF="$(git rev-parse --verify --quiet HEAD 2>/dev/null || true)"
[[ -z "$HEAD_REF" ]] && { echo "stale-base: HEAD 없음(빈 저장소?) — 검사 생략" >&2; exit 0; }
[[ "$HEAD_REF" == "$(git rev-parse "$BASE")" ]] && exit 0  # base 위에 바로 있음 — 최신

BEHIND="$(git rev-list --count "HEAD..$BASE" 2>/dev/null || echo 0)"
[[ "$BEHIND" -eq 0 ]] && exit 0  # main이 우리 커밋을 포함(리베이스/병합 완료 상태)

MSG="stale-base: 브랜치가 $BASE보다 ${BEHIND}커밋 뒤처짐 — PR 생성 전 재기반 필요
  git fetch origin && git rebase $BASE   (또는 git merge $BASE)
충돌 정리 전 PR을 만들면 리뷰·머지가 CONFLICTING으로 무효화된다 (PR #53~58 전멸 사고)."
if [[ "$WARN" == "1" ]]; then
  echo "경고: $MSG" >&2
  exit 0
fi
echo "$MSG" >&2
exit 1
