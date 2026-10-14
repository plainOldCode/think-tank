# TT 아카이브 운영 기준 (NCWZ P3-3 / M4TZHMY7-Q9Y8, 2026-10-14 확정)

## 1. 정본 — probe 스윕(14일) 단일 경로

- **정본**: 서버 내장 `POST /maintenance/archive-sweep` — probe가 사이클당 1회 호출
  (M3ZRGTSF-N95X, PR#77). 기준일 `TT_ARCHIVE_AFTER_DAYS`(mini=**14**).
  서버가 나이순(completed_at ASC)으로 전수 검증 — 보존 행이 뒤 후보를 가리지 않는다.
- **보조**: `tt archive auto`(완료 30일 경과분)는 수동 보조 경로.
  **병용 상호작용**: 스윕이 14일에 먼저 집으면 30일 auto는 대상 0 — 공회전이 정상이다.
  auto를 돌려서 "0건"이 나와도 이상이 아니다.
- 보존 규칙(스윕이 집지 않는 것): ① 조상(부모 체인) 미완료 ② completed_at 없음
  ③ 미달 나이. 보존은 사고가 아니라 설계 동작이다.

## 2. 스윕 실행 증거 추적

**서버 로그**(mini `/tmp/tt-server.log`) — 사이클마다 1행(동작이 있었을 때만):

```
2026-10-14 15:26:26 [archive] 0카드 아카이브, 3보존 (done+14일, 조상 done)
```

**이벤트 로그** — 아카이브 1건마다 `issue.updated` 이벤트(payload `"archived": true, "via": "auto-archive"`).
커서 계약 준수(after_seq=마지막 전달 seq)로 훑기:

```bash
AFTER=0; ARCH=0
while :; do
  R=$(curl -s "$TT_URL/events?after_seq=$AFTER&limit=1000")
  N=$(echo "$R" | python3 -c "import json,sys; print(len(json.load(sys.stdin).get('events',[])))")
  [ "$N" = "0" ] && break
  ARCH=$((ARCH + $(echo "$R" | python3 -c "import json,sys; print(sum(1 for e in json.load(sys.stdin)['events'] if 'archived' in (e.get('payload') or '')))")))
  AFTER=$(echo "$R" | python3 -c "import json,sys; print(json.load(sys.stdin)['cursor'])")
done
echo "누적 archive 이벤트 $ARCH건 (마지막 seq $AFTER)"
```

**2026-10-14 실측**: 전체 2,305 이벤트 훑음 → archive 이벤트 **38건**
(마지막 seq 4305). 서버 로그에서 스윕 주기 실행 확인.

## 3. 잔량 모니터링

**집계 명령** — 14일+ 경과 done-미아카이브(스윕이 이미 집었어야 할 카드):

```bash
curl -s "$TT_URL/issues?limit=1000&archived=all" | python3 -c "
import json,sys
from datetime import datetime,timezone
now=datetime.now(timezone.utc); ts=lambda s: datetime.fromisoformat(s)
ov=[i for i in json.load(sys.stdin) if i['state']=='done' and not i.get('archived')
    and i.get('completed_at') and (now-ts(i['completed_at'])).days>=14]
print(len(ov), [i['id'] for i in ov])"
```

**이상 판정 기준**: 위 잔량이 **보존 사유로 설명되지 않으면** 이상.
- 정상 예(2026-10-14 실측): 잔량 3건 — 전부 부모 `M30W6VBD-N6WE`(todo) 미완료로
  보존 중(조상 체인 규칙). 서버 로그 "3보존"과 수 일치.
- 이상: 잔량 > 0 인데 서버 로그에 보존 행이 없거나, 잔량이 주 단위로 증가.
  → TT_ARCHIVE_AFTER_DAYS 값·스윕 오류 확인, 원인 커밋 조사.

전체 참고치: 10-13 자정 268건 → 10-14 오전 260건 → 10-14 15시 281건(당일 done 급증 —
신규 완료분은 14일 후 자연 소화). 14일+ 경과분만 모니터링 대상.

## 4. 복구 — tt unarchive

```bash
tt unarchive <카드ID>   # archived=0 복귀 (버전 증가, 이벤트 기록)
```

**사용 시점**: 보존·아카이브된 카드를 다시 작업·참조해야 할 때(재발령, 증거 재확인).
복구 후 카드는 done 상태 유지 — 상태 변경은 별도 PATCH. 자주 복구하는 카드가
있다면 보존 규칙(조상 상태)을 먼저 확인할 것.

## 5. 운영 승인 조건 (발령자 사항)

- `TT_ARCHIVE_AFTER_DAYS` 값 변경 (mini launchd env)
- 스윕 비활성(값 0 또는 probe에서 호출 제거)
- 위 둘은 기본값 변경이라 발령자 승인 후 적용. 문서·모니터링·복구는 자유.
