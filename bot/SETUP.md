# Hermes 파일럿 — 4명 전체 (서연우 / 한도윤 / 오지민 / 정하준)

## Discord 채널 + .env

| 채널 | .env 키 |
|------|---------|
| `#서연우-키워드큐` | `DISCORD_CHANNEL_KEYWORD_QUEUE` |
| `#컴플라이언스-검토` | `DISCORD_CHANNEL_COMPLIANCE_REVIEW` |
| `#콘텐츠-검수대기` | `DISCORD_CHANNEL_HUMAN_REVIEW` |
| `#발행대기` | `DISCORD_CHANNEL_PUBLISH` |
| `#오지민-트래킹상태` | `DISCORD_CHANNEL_TRACKING_STATUS` |
| `#전체-긴급알림` | `DISCORD_CHANNEL_BROADCAST` |
| `#개발팀-에러알림` | `DISCORD_CHANNEL_DEV_TEAM` |
| `#정하준-자동조정대기` | `DISCORD_CHANNEL_AUTO_APPLY` |
| `#광고조정-승인필요` | `DISCORD_CHANNEL_HUMAN_APPROVAL` |

`bot/.env.example` → `~/.hermes/.env`에 채운 뒤:

```bash
cd yn_agent
python -m bot.main
```

봇 재시작 후 Discord도 재실행해야 슬래시커맨드 4개가 보입니다.

## 슬래시커맨드 테스트

| 커맨드 | 직원 | 결과 |
|--------|------|------|
| `/run_keyword_scan` | 서연우 | 키워드큐 / 컴플라이언스 |
| `/run_content_draft` | 한도윤 | 검수대기 / 발행대기 (또는 키워드 **승인**으로 자동) |
| `/run_anomaly_check` | 오지민 | critical→긴급알림 / warning→트래킹상태 / normal→조용 / privacy→개발팀 |
| `/run_bid_review` | 정하준 | 자동조정대기 / 승인필요 / 민감→컴플라이언스 병행 |

### 정하준 tracking 옵션

- `normal` — 제안 생성 (±20% 이내→자동조정, 초과/PAUSE→승인필요, 성범죄→컴플라이언스 병행)
- `warning` — 제안 + `caution_flag`
- `critical` — 제안 안 함, `#개발팀-에러알림`에 보류 알림
- `db/기본` — DB `tracking_status` 사용 (없으면 Normal)

### 추천 시연 순서

```
/run_keyword_scan → 키워드 승인 → 한도윤 초안 확인
/run_anomaly_check (critical) → 긴급알림
/run_bid_review (critical) → 정하준 보류 확인
/run_anomaly_check (normal) → DB Normal
/run_bid_review (normal) → 자동조정 / 승인필요 / 컴플라이언스
```

## DB 확인

```bash
sqlite3 bot/hermes.db "SELECT id, agent_id, routed_to FROM agent_memory ORDER BY id DESC LIMIT 20;"
sqlite3 bot/hermes.db "SELECT status, updated_at FROM tracking_status;"
```
