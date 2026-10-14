# backlog 주기 triage — 분류표·승격 기준 (Q0D1/M4TZJ09V, 2026-10-14 초기 산출)

주기: **주 1회**(월요일 권장). 이 문서가 입력 — 다음 triage는 이 표를 갱신한다.

## 1. 승격 기준 (발령자 승인 전제)

- **인프라/TT개선 후보**: 재발 실측 근거(로그·횟수·영향 카드 ID)가 붙을 때만 todo 승격.
  근거 없는 승격 금지. 후보 라벨(`candidate` 등) 유지.
- **서비스 카드**: 해당 프로젝트 마더 카드 산하로 이관(PATCH parent_id) — 단,
  아래 표에서 확인된 대로 **기존 부모 구조가 이미 양호하면 이관 0건**이 정상이다.
- **아이디어/실험/검토**: backlog 유지 — 승격·이관 없음. 종결·무효화 시 아카이브.

## 2. 전수 분류표 (2026-10-14 실측, backlog 53건 — ID당 1행)

### 서비스(armour) — 24건 (부모 구조 양호, 이관 0)

| ID | 현재 부모 | 제목 요지 | 근거·처리 |
|---|---|---|---|
| M4MA974F-W546 | - | 업무·인프라 백로그 마더 | 마더 자체 |
| M4PPMVGB-0NTP | - | ARMOUR 10월 목표 | 마더 자체 |
| M4Q9VWFA-PN6P | M4PPMVGB-0NTP | SJ11 후속 실데이터 전환 | 10월 목표 산하 |
| M4W5T61R-KMRY | M4Q9VWFA-PN6P | (회의필요) 사진 보존·삭제 원칙 후속 | API/Web 체인 유지 |
| M4PPC53E-W6DT | M4Q9VWFA-PN6P | S3 호환 첨부 저장소 | API 체인 유지 |
| M4MCQ36D-HD8F | M4Q9VWFA-PN6P | 사진 수정·삭제(소프트 삭제) | API 체인 유지 |
| M4PPSFM5-J1FB | M4PPMVGB-0NTP | [10월-01] 사용자 역할·IA | 10월 WBS |
| M4PPSYS7-VTXQ | M4PPMVGB-0NTP | [10월-08] 릴레이·데이터 허브 계약 | 10월 WBS |
| M4PPTE27-W37P | M4PPMVGB-0NTP | [10월-10] 통합 아키텍처 | 10월 WBS |
| M4PPTJR6-XWGC | M4PPMVGB-0NTP | [10월-12] Phase 2 상세 설계 | 10월 WBS |
| M4PPTN8C-9HZC | M4PPMVGB-0NTP | [10월-13] 테일러링 산출물 통합 | 10월 WBS |
| M4PPTQNR-VT5K | M4PPMVGB-0NTP | [10월-14] 고객 리뷰 환경 | 10월 WBS |
| M4PPTWDH-F0N4 | M4PPMVGB-0NTP | [10월-15] WBS 실적·지연 대응 | 10월 WBS |
| M31D7XP2-90Z9 | M4MA974F-W546 | [A12] 서비스 운영 온톨로지 | A시리즈 |
| M31D7XPZ-5G53 | M4MA974F-W546 | [A03] WBS 진행·증빙 갱신 | A시리즈 |
| M31D7XQT-7AR6 | M4MA974F-W546 | [9월 목표] 목업 배포+API 연결 | A시리즈(종료분 정리 권고) |
| M31D7XRM-QBPE | M4MA974F-W546 | [A08] 구두·서면 피드백 대조 | A시리즈 |
| M31D7XSK-SYS1 | M4MA974F-W546 | [A07] 사용자 동선·시나리오 | A시리즈 |
| M31D7XTC-ADMY | M4MA974F-W546 | [검토] 목업 접속 기록 수집 | A시리즈 |
| M31D7XV8-ZVAN | M4MA974F-W546 | [아이디어] 메일 LLM 파이프라인 | A시리즈 아이디어 |
| M31DEA81-8D4X | M4MA974F-W546 | [아이디어] 피드백 프레임워크 | A시리즈 아이디어 |
| M327Y5XD-EEPW | M4MA974F-W546 | [Super Admin] 사용자 등록 | armour 백로그 |
| M3VEMTXX-VVGC | M4MA974F-W546 | gitea main·배포 branch 정리 | armour 인프라 |
| M31FA31S-MXKD | M4MA97G8-9G00 | armour-wiki-sync 스킬 교정 | 지식관리 마더 산하(스킬 인프라 성격 — 현위치 유지) |

### 서비스(지식관리) — 1건

| ID | 현재 부모 | 제목 요지 | 근거·처리 |
|---|---|---|---|
| M4MA97G8-9G00 | - | 지식·분석 백로그 마더 | 마더 자체 |

### 인프라(활성) — 1건

| ID | 현재 부모 | 제목 요지 | 근거·처리 |
|---|---|---|---|
| M4TTJGKK-PKAR | - | tt 강화(모자) | 잔여 W098·Q0D1·M3DP |

### 인프라 후보 — 2건 (근거 시 승격)

| ID | 현재 부모 | 제목 요지 | 근거·처리 |
|---|---|---|---|
| M3ETHP9A-X12P | M4TTJGKK-PKAR | runner 워크스페이스 수명주기 | candidate 라벨 — 재발 실측 대기 |
| M3DNJXT1-G34D | M4MA97G8-9G00 | openclaw test-audit 제약 | constraint-candidate — 재발 실측 대기 |

### 아이디어·실험·검토 — 25건 (유지)

| ID | 현재 부모 | 제목 요지 | 근거·처리 |
|---|---|---|---|
| M30W6VC0-1JNQ | M30W6VBD-N6WE | 실루엣 티저 수집 구조 | 게임/콘텐츠 기획군 — 부모 산하 유지 |
| M30W6VC7-BNWS | M30W6VBD-N6WE | 아트/캐릭터 전략·디스커버리 | 〃 |
| M30W6VCF-1EAK | M30W6VBD-N6WE | 제작 워크플로 확정 | 〃 |
| M30W6VCQ-D816 | M30W6VBD-N6WE | P2P+LLM 상대전 구조 설계 | 〃 |
| M30W6VCZ-TKSB | M30W6VBD-N6WE | AI 대리 포켓몬 프레이밍 | 〃 |
| M30W6VD7-8TY4 | M30W6VBD-N6WE | 경쟁 사례 연구(LLM 관전 아레나) | 〃 |
| M30W6VDF-S1T4 | M30W6VBD-N6WE | 게임 재미 연구 문헌 | 〃 |
| M30W6VDQ-2DED | M30W6VBD-N6WE | 숏폼 바이럴·수익화 | 〃 |
| M30W6VDY-9PBQ | - | 오마키→크롬캐스트 미러링 | omarchy 로컬 개선 |
| M439DGR2-F4F5 | M4TTJGKK-PKAR | [idea] TT 이슈 ETA 휴리스틱 | idea — 유지 |
| M36EXDJM-B7VV | M36CPFTP-7CEC(done) | TTS 교체 벤치(Piper/Kokoro) | voice 검토군 |
| M425EW2Q-48CM | - | 보이스 기록→TT 카드 파이프라인 | voice군(HSP3가 사실상 마더) |
| M425EW7C-HSP3 | - | 실시간 대화 파이프라인 | voice군 루트 |
| M425F99D-KSTQ | M425EW7C-HSP3 | 핫워드 상시감지 | voice군 |
| M425F9DV-4004 | M425EW7C-HSP3 | 발화 중단(barge-in) | voice군 |
| M45PKYTG-5PZG | M425EW7C-HSP3 | LiveKit Agents 검토 | voice군 |
| M4Q734W4-0VC3 | M425EW7C-HSP3 | Voxt(sglang-omni) 분석 | voice군 |
| M4GQ3JE4-FBEM | - | [실험#3] effort 매트릭스 확장 | 실험군 루트 |
| M4GQ1HSK-P7KQ | M4GQ3JE4-FBEM | 108셀 실측 | 실험군 |
| M4GQ3JHZ-TFFZ | M4GQ3JE4-FBEM | GLM-5.3 Flash 매트릭스 | 실험군 |
| M4GQ3JNR-7FR1 | M4GQ3JE4-FBEM | codex 매트릭스 | 실험군 |
| M4GQ3JSC-EGEA | M4GQ3JE4-FBEM | claude 매트릭스 | 실험군 |
| M4GQ3JXF-93XB | M4GQ3JE4-FBEM | agy 매트릭스 | 실험군 |
| M345KSPG-CB2W | M4MA97G8-9G00 | Knowledge Wiki→Model | 지식관리 검토 |
| M4PSGCTE-MH9F | - | T-STATION 분석 | **귀속 불명 — 마더 결정 필요(발령자)** |

## 3. 2026-10-14 triage 결론

- **승격 후보 0건** — 재발 실측 근거가 붙은 인프라 후보 없음(근거 없는 승격 금지 준수).
- **이관 필요 0건** — 서비스군 25건 전부 기존 마더 체인(W546·0NTP·9G00)에 정상 귀속.
  이전 초안의 "이관 22건"은 부모 확인 전 추정이었고, 실측에서 철회된다.
- **발령자 결정 1건**: M4PSGCTE-MH9F(T-STATION) 프로젝트 귀속 — 마더 미정.
- **참고**: 31D7* 9월 A시리즈는 장기 미착수 — 부모는 정상이나 마더에서 우선순위
  재판정 권고. M30W6V 게임 기획군은 부모 N6WE(todo) 산하 유지.

## 4. 다음 triage 입력

- 이 문서 §2 표 갱신(신규 backlog 카드 추가, 상태 변경 행 제거, 부모 이동 반영)
- W098(10-24 창 종료)의 5종 지표 — 승격 근거 판정에 활용
- MH9F 귀속 결정 반영
