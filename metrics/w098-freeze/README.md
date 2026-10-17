# W098 동결 스냅샷 (M4TZHACD-W098 재현용)

- `issues-20261017T1915KST.json`: GET /issues?limit=1000 동결(2026-10-17 19:15 KST) — 스냅샷 지표(재작업 attempt≥2, stall active)의 원천. 지표 산출 필드만 보존(id·state·execution_attempt·started_at·completed_at) — secret-scan 준수를 위해 title/body 등 텍스트 필드는 제거했다.
- `tt_metrics_state.json`: stall episode 영속 상태 동결(같은 시각) — resolved 2건(HBXY 90분, QN0H 30분).

## 재현 명령

```bash
# 이벤트 파생 지표(착수·리뷰통과·개입) — 컷 seq≤5684 (2026-10-17T14:50:47+0900)
# 운영 서버 주소는 TT_URL 환경변수로 전달(레포에 기록하지 않는다 — K4Q7).
export TT_URL=https://<운영 서버>
python3 scripts/metrics_collect.py \
  --base "$TT_URL" --max-seq 5684 \
  --window-start '2026-10-10T00:00:00+09:00' \
  --state-file metrics/w098-freeze/tt_metrics_state.json \
  --issues-file metrics/w098-freeze/issues-20261017T1915KST.json --json
```
