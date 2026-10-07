#!/usr/bin/env bash
# secret-scan.sh — push 전 tracked 파일에서 민감 패턴을 찾는다. 히트 시 exit 1.
# 사용: scripts/secret-scan.sh [repo-root]   (tt push가 자동 호출)
# 개인·환경 고유 패턴은 gitignored scripts/secret-patterns.local 에 한 줄씩.
# TT_SCAN_DIFF=<ref>: 전체 대신 <ref> 이후 추가된 줄만 스캔. 이미 공개된 과거
# 히스토리는 새 push를 막지 않지만, 신규 내용은 여전히 차단한다. 미설정 시 전체.
set -uo pipefail
cd "${1:-.}"
PATTERNS=(
  '100\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}'   # tailscale/100net 주소
  '\b(?![\w.-]*example\.ts\.net)[a-zA-Z0-9._-]+\.ts\.net\b' # 실제 tailscale 주소 (example.ts.net 허용)
  '/Users/(?!YOU(/|$))[A-Za-z0-9._-]+'        # 실제 홈 경로 (YOU placeholder 허용)
  '/home/(?!YOU(/|$))[a-z0-9._-]+'
  'ghp_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{20,}' # GitHub 토큰
  'sk-[A-Za-z0-9_-]{20,}'                     # API 키 (OpenAI 등)
  '-----BEGIN (?:RSA|EC|OPENSSH|DSA|PGP) PRIVATE KEY-----' # 프라이빗 키
  '<<<<<<< |>>>>>>> '                                       # unresolved conflict marker
)
if [[ -f scripts/secret-patterns.local ]]; then
  while IFS= read -r line; do [[ -n "$line" && ! "$line" == \#* ]] && PATTERNS+=("$line"); done < scripts/secret-patterns.local
fi
hits=0
if [[ -n "${TT_SCAN_DIFF:-}" ]]; then
  # 추가된 줄만(+ 접두, +++ 헤더 제외). 파일명 보존. 기준이 HEAD면 uncommitted까지 포함.
  for pat in "${PATTERNS[@]}"; do
    out=""
    for f in $(git diff --name-only "$TT_SCAN_DIFF" 2>/dev/null); do
      case "$f" in .venv/*|scripts/secret-scan.sh|scripts/secret-patterns.local) continue ;; esac
      add="$(git diff "$TT_SCAN_DIFF" -- "$f" | grep '^+' | grep -v '^+++' | sed "s|^+|$f:+|" || true)"
      hit="$(printf '%s\n' "$add" | PAT="$pat" perl -ne 'print if m/$ENV{PAT}/' 2>/dev/null | head -10 || true)"
      [[ -n "$hit" ]] && out+="$hit"$'\n'
    done
    if [[ -n "$out" ]]; then
      echo "✗ 패턴 [$pat] — ${TT_SCAN_DIFF} 이후 추가된 줄:"
      printf '%s' "$out"
      hits=1
    fi
  done
else
  for pat in "${PATTERNS[@]}"; do
    # git grep -P는 git 빌드에 pcre가 없으면(예: Apple CLT git) 사용 불가 — ls-files+perl로 이식
    # -z + read -d '': core.quotePath 인용/탭/역슬래시 파일명 보존 (리뷰 #2) — grep -- 로 옵션 해석 방지
    out="$(git ls-files -z -- ':!.venv' ':!scripts/secret-scan.sh' ':!scripts/secret-patterns.local' | while IFS= read -r -d '' f; do
      [ -f "$f" ] || continue
      [ -L "$f" ] && continue  # 심볼릭 링크 대상은 repo 밖일 수 있음 — 내용 출력 자체가 유출 경로 (리뷰 #6)
      [[ "$f" == -* ]] && f="./$f"  # 단독 `-`는 grep/perl에서 stdin 의미 — 일반 파일 경로로 정규화 (리뷰 #5). case는 $( ) 안에서 파서 충돌
      grep -qI . -- "$f" 2>/dev/null || continue
      PAT="$pat" perl -e '
        my $f = $ARGV[0];
        open(my $fh, "<", $f) or exit 0;  # 3-arg open — two-arg `<>`는 `|`로 끝나는 파일명을 명령으로 실행함(리뷰 P1)
        my $n = 0;
        while (my $l = <$fh>) { $n++; print "$f:$n:$l" if $l =~ m/$ENV{PAT}/; }
      ' -- "$f"  # -- : -credential.txt 같은 이름의 옵션 오해 방지 (리뷰 #2)
    done)"
    if [[ -n "$out" ]]; then
      echo "✗ 패턴 [$pat]:"
      echo "$out" | head -10
      hits=1
    fi
  done
fi
[[ $hits -eq 1 ]] && { echo "secret-scan 실패 — 위 항목 스럽 후 재시도"; exit 1; }
echo "secret-scan 통과"
