#!/usr/bin/env bash
# CJS6(M54X0YH2-CJS6) — 배포 시 장부·태그·스킬·문서를 동시 갱신한다.
# CHANGELOG가 10-14에서 멈추고 CLI(2.5.3 vs 2.7.2)·스킬·문서가 드리프트한 사고의 재발 방지.
#
# 사용:
#   TT_URL=http://<host>:7800 scripts/deploy.sh [--dry-run] "배포 내용 요약 (커밋)"
#   scripts/deploy.sh --dry-run "9PDD 병합 bed03a1"   # 실제 변경 없이 절차 미리보기
#
# 수행: openapi 해시 측정 → CHANGELOG 1행 추가 → annotated tag → 스킬 사본·
#       SKILL.md 메타데이터 동기화 → 문서 패리티 테스트 → CLI 재설치 안내.
# CLI 재설치·실배포는 사용자 승인 후 scripts/tt-reinstall-gate.sh로 진행한다.
set -euo pipefail

DRY=0
if [ "${1:-}" = "--dry-run" ]; then DRY=1; shift; fi
NOTE="${1:?배포 내용 요약을 인자로 — scripts/deploy.sh [--dry-run] \"요약\"}"
: "${TT_URL:?TT_URL을 설정할 것 (예: TT_URL=http://<host>:7800)}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

HASH=$(curl -sf "$TT_URL/openapi.json" | shasum -a 256 | cut -c1-8)
VER=$(sed -n 's/^TT_CLI_VERSION="\?\([0-9.]*\)"\?/\1/p' cli/tt | head -1)
DATE=$(date +%F)
TAG="deploy-cli${VER}-openapi${HASH}"
ROW="- ${DATE}: cli ${VER}, openapi **${HASH}** — ${NOTE}. 태그 \`${TAG}\`."

echo "== 배포 절차 (dry-run=$DRY) =="
echo "openapi 해시: $HASH   cli 버전: $VER   태그: $TAG"
echo "CHANGELOG 행: $ROW"

if [ "$DRY" = 1 ]; then
  echo "(dry-run — 파일 변경·태그 없음)"
  exit 0
fi

# 1) CHANGELOG 1행 추가 (## 배포 이력 바로 아래)
python3 - "$ROW" <<'PY'
import sys
row = sys.argv[1]
p = "CHANGELOG.md"
s = open(p).read()
marker = "## 배포 이력\n\n"
assert marker in s, "CHANGELOG.md에 ## 배포 이력 절 없음"
if row in s:
    print("CHANGELOG: 이미 기록됨 — 스킵")
else:
    s = s.replace(marker, marker + row + "\n", 1)
    open(p, "w").write(s)
    print("CHANGELOG: 1행 추가")
PY

# 2) annotated tag (동일 태그 재실행은 스킵)
if git rev-parse -q --verify "refs/tags/$TAG" >/dev/null; then
  echo "태그: 이미 존재 — 스킵"
else
  git tag -a "$TAG" -m "cli ${VER} openapi ${HASH} — ${NOTE}"
  echo "태그: 생성됨 — push: git push origin $TAG"
fi

# 3) 스킬 사본·SKILL.md 메타데이터 동기화
cp server/static/api.md skills/tt/references/api.md
python3 - "$VER" "$HASH" <<'PY'
import re, sys
ver, hash8 = sys.argv[1], sys.argv[2]
p = "skills/tt/SKILL.md"
s = open(p).read()
s = re.sub(r'  cli_version: "[^"]*"', f'  cli_version: "{ver}"', s)
s = re.sub(r'  openapi_hash: "[^"]*"', f'  openapi_hash: "{hash8}"', s)
if 'cli_version' not in s:
    s = s.replace('  openapi: "/openapi.json"',
                  f'  openapi: "/openapi.json"\n  cli_version: "{ver}"\n  openapi_hash: "{hash8}"')
open(p, "w").write(s)
print("SKILL.md: cli_version=%s openapi_hash=%s" % (ver, hash8))
PY

# 4) 문서 패리티 검증 — 어긋나면 배포 중단
python3 -m pytest tests/test_contract_docs.py -q

echo "== 완료 — 다음 단계(수동) =="
echo "1) 커밋·푸시: CHANGELOG.md·skills/ 변경분"
echo "2) 사용자 승인 후 에이전트 박스 CLI 재설치: scripts/tt-reinstall-gate.sh"
echo "3) 검증: tt version → 해시 $HASH 일치 확인"
