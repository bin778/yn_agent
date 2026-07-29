# Hermes 파일럿 — 시연 스크립트 (4명 한 장)

서연우 / 한도윤 / 오지민 / 정하준 Discord 파일럿 실행·시연·권한 정리.

상세 큐 정의: [`_shared/queues.md`](../_shared/queues.md) · 역할: [`_shared/roles.md`](../_shared/roles.md)

---

## 1. 사전 준비

1. [`bot/.env.example`](./.env.example)을 참고해 `~/.hermes/.env`에 토큰·채널 ID 채우기
2. 아래 **채널 9개** 생성 + **권한 체크리스트** 적용
3. 실행:

```bash
cd yn_agent
python -m bot.main
```

4. 봇 재시작 후 Discord도 재실행 → 슬래시커맨드 4개 확인

---

## 2. Discord 채널 ↔ .env

| 채널                   | .env 키                             | 민감도          |
| ---------------------- | ----------------------------------- | --------------- |
| `#서연우-키워드큐`     | `DISCORD_CHANNEL_KEYWORD_QUEUE`     | 일반            |
| `#컴플라이언스-검토`   | `DISCORD_CHANNEL_COMPLIANCE_REVIEW` | **제한**        |
| `#콘텐츠-검수대기`     | `DISCORD_CHANNEL_HUMAN_REVIEW`      | **제한**        |
| `#발행대기`            | `DISCORD_CHANNEL_PUBLISH`           | 일반(+발행담당) |
| `#오지민-트래킹상태`   | `DISCORD_CHANNEL_TRACKING_STATUS`   | 일반            |
| `#전체-긴급알림`       | `DISCORD_CHANNEL_BROADCAST`         | 개발+관련자     |
| `#개발팀-에러알림`     | `DISCORD_CHANNEL_DEV_TEAM`          | 개발+관련자     |
| `#정하준-자동조정대기` | `DISCORD_CHANNEL_AUTO_APPLY`        | 일반            |
| `#광고조정-승인필요`   | `DISCORD_CHANNEL_HUMAN_APPROVAL`    | 팀장+마케팅     |

---

## 3. 채널·권한 체크리스트 (Discord 서버에서 수동)

봇 코드는 채널에 **게시**만 합니다. 누가 볼지는 **서버 채널 권한**으로 막습니다.

### 3-1. 역할(Role) 생성 (`_shared/roles.md`)

- [ ] `@마케팅팀장` (`marketing_lead`)
- [ ] `@법무컴플라이언스` (`legal_compliance`)
- [ ] `@콘텐츠검수변호사` (`attorney_reviewer`)
- [ ] `@개발팀` (`dev_team`)
- [ ] `@발행담당자` (`publish_operator`)
- [ ] (선택) role ID를 `roles.md`의 `discord_role_id`에 기입

### 3-2. Private 채널 — 법무·팀장·변호사만

`#컴플라이언스-검토`, `#콘텐츠-검수대기`:

- [ ] 채널을 Private로 설정
- [ ] `@everyone` / 일반 멤버 **보기·전송 제거**
- [ ] 허용: `@법무컴플라이언스`, `@콘텐츠검수변호사`(검수대기), `@마케팅팀장`, **Yeoon Agent 봇**
- [ ] 봇에게는 최소한: 메시지 보기, 메시지 보내기, 임베드 링크, (버튼용) 앱 사용

### 3-3. 그 외 권장

| 채널                                 | 볼 수 있는 사람                                              |
| ------------------------------------ | ------------------------------------------------------------ |
| `#광고조정-승인필요`                 | `@마케팅팀장`, 마케팅팀, 봇                                  |
| `#전체-긴급알림`, `#개발팀-에러알림` | `@개발팀` + 관련자, 봇                                       |
| `#발행대기`                          | `@발행담당자` + 마케팅, 봇                                   |
| 나머지 큐 채널                       | 마케팅팀 전체 열람 가능 (승인 버튼은 담당자만 누르도록 운영) |

시범 서버라도 **컴플라이언스·검수 두 채널만이라도** Private로 잠그는 것을 권장합니다.

---

## 4. 슬래시커맨드 (즉시 테스트)

| 커맨드               | 직원   | 결과 채널                                                             |
| -------------------- | ------ | --------------------------------------------------------------------- |
| `/run_keyword_scan`  | 서연우 | 키워드큐 / 컴플라이언스                                               |
| `/run_content_draft` | 한도윤 | 검수대기 / 발행대기 (키워드 **승인**으로도 자동)                      |
| `/run_anomaly_check` | 오지민 | critical→긴급알림 / warning→트래킹상태 / normal→조용 / privacy→개발팀 |
| `/run_bid_review`    | 정하준 | 자동조정대기 / 승인필요 / 민감→컴플라이언스 병행                      |

### 정하준 `tracking` 옵션

- `normal` — 제안 생성
- `warning` — 제안 + `caution_flag`
- `critical` — 제안 보류 → `#개발팀-에러알림`
- `db/기본` — DB `tracking_status` (없으면 Normal)

### 스케줄 (봇이 켜져 있을 때, 슬래시 없이)

| 직원          | 시각 (KST)   |
| ------------- | ------------ |
| 정하준        | 매일 08:30   |
| 서연우        | 매일 09:00   |
| 한도윤 백로그 | 10:00, 15:00 |
| 오지민        | 30분마다     |

---

## 5. 승인 경로 맵 (시연 핵심)

| 단계 | 어디서                                        | 버튼    | 다음에 일어나는 일                                                |
| ---- | --------------------------------------------- | ------- | ----------------------------------------------------------------- |
| A    | `#서연우-키워드큐`                            | ✅ 승인 | DB 승인 기록 → **한도윤 초안 자동 실행** → 검수대기 또는 발행대기 |
| A'   | `#서연우-키워드큐`                            | ❌ 반려 | DB 반려만 (한도윤 안 감)                                          |
| B    | `#컴플라이언스-검토`                          | ✅/❌   | DB 기록만 (법무 검토 시연). 자동 핸드오프 없음                    |
| C    | `#콘텐츠-검수대기`                            | ✅/❌   | DB 기록만 (변호사 검수 시연). CMS 발행 없음                       |
| D    | `#발행대기`                                   | ✅/❌   | DB 기록만. 실제 웹 발행 없음                                      |
| E    | `#정하준-자동조정대기` / `#광고조정-승인필요` | ✅/❌   | DB 기록만. Ads 실제 반영 없음                                     |

**자율로 이어지는 유일한 AI 핸드오프**: A의 키워드 승인 → 한도윤.  
나머지는 설계상 **사람 승인 = 기록**, 실제 실행(발행/광고)은 아직 연결하지 않음.

---

## 6. 추천 시연 순서 (5~10분)

```
1. /run_keyword_scan
   → #서연우-키워드큐 일반 키워드 ✅ 승인 → 한도윤 초안이 검수/발행 채널에 올라옴
   → #컴플라이언스-검토 high-risk 항목 확인

2. /run_anomaly_check  scenario: critical
   → #전체-긴급알림 (+ #개발팀-에러알림)

3. /run_bid_review  tracking: critical
   → 정하준 보류 알림 (#개발팀-에러알림)

4. /run_anomaly_check  scenario: normal   (DB tracking = Normal)
5. /run_bid_review  tracking: normal
   → #정하준-자동조정대기 / #광고조정-승인필요 / (민감) #컴플라이언스-검토
```

---

## 7. DB 확인

```bash
sqlite3 bot/hermes.db "SELECT id, agent_id, routed_to, human_decision FROM agent_memory ORDER BY id DESC LIMIT 20;"
sqlite3 bot/hermes.db "SELECT status, updated_at FROM tracking_status;"
```

---

## 문제 해결

- 채널에 안 올라옴 → 콘솔 `[WARN]` / 채널 ID / 봇이 해당 채널에 있는지·메시지 권한
- 슬래시커맨드 1개만 보임 → 봇·Discord 둘 다 재시작 (동기화 캐시)
- Private 채널에 봇이 못 씀 → 해당 채널에 봇 role 명시적 허용
- `Privileged message content intent` 경고 → 슬래시·버튼 시연에서는 무시 가능
