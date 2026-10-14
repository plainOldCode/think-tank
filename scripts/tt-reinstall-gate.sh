#!/bin/sh
# CLI 재설치 게이트 (JK8Z/M4TZHMST-JK8Z) — openapi 해시 불일치 시에만 재설치한다.
#
# 배경: 10-12~13 이틀간 CLI 3회 재설치(2.4.0→2.5.1→2.5.3)의 재발 방지. 재설치가
# 의미 있는 경우는 서버 openapi 해시가 직전 설치와 불일치할 때뿐이다(NCWZ 롤백
# 기준과 동일 근거). 일치하면 no-op. 파싱·fetch 실패 시 재설치하지 않고 비정상
# 종료 — 불확실할 때 손대지 않는다(보수적 유지).
#
# 환경: TT_URL(서버, 기본 http://127.0.0.1:7800). 기록: ~/.local/state/tt/
# installed-openapi. 로그: ~/.local/state/tt/reinstall-gate.log
# cron/launchd 등록은 발령자 승인 후 별도(카드 비범위).
set -u

TT_URL="${TT_URL:-http://127.0.0.1:7800}"
STATE_DIR="$HOME/.local/state/tt"
RECORD="$STATE_DIR/installed-openapi"
LOG="$STATE_DIR/reinstall-gate.log"

log() { mkdir -p "$STATE_DIR"; echo "$(date '+%F %T') $1" >> "$LOG"; }
fail() { log "FAIL: $1"; echo "gate FAIL: $1" >&2; exit 3; }

# 1. tt version 파싱 — cli 버전·server openapi 해시
V="$(tt version 2>/dev/null)" || fail "tt version 실패"
HASH="$(printf '%s\n' "$V" | sed -n 's/.*openapi \([0-9a-f]\{8\}\).*/\1/p')"
CLI="$(printf '%s\n' "$V" | sed -n 's/.*cli \([0-9.]*\).*/\1/p')"
[ -n "$HASH" ] || fail "openapi 해시 파싱 실패 — '$V'"
[ -n "$CLI" ] || fail "cli 버전 파싱 실패 — '$V'"

# 2. 직전 설치 해시 대조 — 일치하면 no-op
if [ -f "$RECORD" ] && [ "$(cat "$RECORD" 2>/dev/null)" = "$HASH" ]; then
  log "OK: no-op (cli $CLI openapi $HASH 일치)"
  echo "no-op: openapi $HASH 일치"
  exit 0
fi

# 3. 불일치(또는 기록 부재) → 서버가 구워낸 /install.sh로 재설치
TMP="$(mktemp)" || fail "mktemp 실패"
if ! curl -sS --max-time 30 -o "$TMP" "$TT_URL/install.sh"; then
  rm -f "$TMP"; fail "install.sh fetch 실패 ($TT_URL)"
fi
if ! sh "$TMP" >/dev/null 2>&1; then
  rm -f "$TMP"; fail "install.sh 실행 실패"
fi
rm -f "$TMP"

# 4. 재검증 — 설치된 tt가 같은 해시를 보고해야 갱신한다
V2="$(tt version 2>/dev/null)" || fail "재검증 tt version 실패"
HASH2="$(printf '%s\n' "$V2" | sed -n 's/.*openapi \([0-9a-f]\{8\}\).*/\1/p')"
[ -n "$HASH2" ] || fail "재검증 파싱 실패 — '$V2'"
[ "$HASH2" = "$HASH" ] || fail "재검증 불일치 ($HASH != $HASH2)"

mkdir -p "$STATE_DIR"
printf '%s' "$HASH2" > "$RECORD"
log "REINSTALLED: cli $CLI openapi $HASH"
echo "reinstalled: cli $CLI openapi $HASH"
