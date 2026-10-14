# backlog 주기 triage — 분류표·승격 기준 (Q0D1/M4TZJ09V, 2026-10-14 초기 산출)

주기: **주 1회**(월요일 권장). 이 문서가 입력 — 다음 triage는 이 표를 갱신한다.

## 1. 승격 기준 (발령자 승인 전제)

- **인프라/TT개선 후보**: 재발 실측 근거(로그·횟수·영향 카드 ID)가 붙을 때만 todo 승격.
  근거 없는 승격 금지. 후보 라벨(`candidate` 등) 유지.
- **서비스 카드**: 해당 프로젝트 마더 카드로 이관(PATCH parent_id) — TT backlog에 두지 않는다.
- **아이디어/실험/검토**: backlog 유지 — 승격·이관 없음. 단, 종결·무효화 시 아카이브.

## 2. 전수 분류표 (2026-10-14 실측, backlog 52건)

### 서비스 — 마더 이관 대상 (승격 아님, parent_id 이관)

| ID | 제목 요지 | 이관 마더 | 근거 |
|---|---|---|---|
| M4PPMVGB-0NTP | ARMOUR 10월 목표 | 자체가 마더 | 유지 |
| M4PPTWDH~M4PPSFM5 (7건) | [10월-01~15] WBS·설계·IA·리뷰·검증 | M4PPMVGB-0NTP | ARMOUR 10월 WBS군 |
| M4W5T61R-KMRY | (회의필요) 사진 보존·삭제 원칙 | M4MA974F-W546 | mockup-to-api 후속 |
| M4Q9VWFA-PN6P | SJ11 후속 실데이터 전환 | M4MA974F-W546 | mockup-to-api |
| M4PPC53E-W6DT | S3 호환 첨부 저장소 | M4MA974F-W546 | API |
| M4MCQ36D-HD8F | 사진 수정·삭제(소프트) | M4MA974F-W546 | API/Web |
| M327Y5XD-EEPW | Super Admin 사용자 등록 | M4MA974F-W546 | armour 백로그 |
| M31D7XP2-90ZB 외 31D7* 6건 | A12 온톨로지·A03 WBS 증빙·9월 목표·A08·A07·접속기록 | M4MA974F-W546 | 9월 A시리즈 — 장기 미착수 |
| M31D7XV8-ZVAN | 서비스 메일 LLM 파이프라인(아이디어) | M4MA974F-W546 | armour 아이디어 |
| M31DEA81-8D4X | 목업 피드백 프레임워크(아이디어) | M4MA974F-W546 | armour 아이디어 |
| M31FA31S-MXKD | armour-wiki-sync 스킬 교정 | M4MA974F-W546 | armour 인프라 |

### 인프라/TT개선 후보 — 유지(근거 시 승격)

| ID | 제목 요지 | 상태 |
|---|---|---|
| M3ETHP9A-X12P | runner 작업 워크스페이스 수명주기 | 후보 유지 — 재발 실측 근거 대기 |
| M3DNJXT1-G34D | openclaw test-audit 테스트 제약 | 후보 유지 |
| M439DGR2-F4F5 | TT 이슈 ETA 휴리스틱([idea]) | 유지 |
| M4TTJGKK-PKAR | tt 강화(모자) | 진행 중 자체 — 2026-10-14 기준 잔여 W098·Q0D1·M3DP |

### 아이디어/실험/검토 — 유지

| ID | 제목 요지 |
|---|---|
| M4Q734W4-0VC3 | Voxt(sglang-omni) 분석 |
| M4PSGCTE-MH9F | T-STATION 분석 |
| M45PKYTG-5PZG | LiveKit Agents 검토 |
| M425F99D-KSTQ | 핫워드 상시감지 |
| M425F9DV-4004 | 발화 중단(barge-in) |
| M425EW2Q-48CM | 보이스 기록→TT 카드 파이프라인 |
| M425EW7C-HSP3 | 실시간 대화 파이프라인 |
| M36EXDJM-B7VV | TTS 교체 벤치(Piper/Kokoro) |
| M4GQ3JNR-7FR1 외 5건 | effort 매트릭스 실험군(7FR1·EGEA·93XB·FBEM·TFFZ·P7KQ) |
| M345KSPG-CB2W | Knowledge Wiki → Knowledge Model |
| M4MA97G8-9G00 | 지식관리 마더(자체) |
| M30W6VC0-1JNQ | 실루엣 티저 수집 구조 |

## 3. 2026-10-14 triage 결론

- **승격 후보: 0건** — 재발 실측 근거가 붙은 인프라 후보 없음(근거 없는 승격 금지 준수).
- **이관 후보: 22건**(위 표 서비스군) — 전부 ARMOUR 계열. 이관 실행은 발령자 승인 후
  `tt edit ID -p <마더>` 1회 배치. 이관해도 상태·본문 불변(부모만 변경).
- **보류 사유**: 31D7* 9월군은 2026-09-30 목표 종료 후 미착수 — 이관하되 마더에서
  우선순위 재판정 권고. voice/실험군은 관심사 유지(별도 마더 없음 — 필요 시 신설).

## 4. 다음 triage 입력

- 이 문서 §2 표의 갱신(신규 backlog 카드 추가·이관 완료 행 제거)
- W098(10-24 창 종료)의 5종 지표 — 승격 근거 판정에 활용
