# Hermes 파일럿 — 로컬 실행 가이드 (서연우 / 한도윤 / 오지민)

## 1. 폴더 배치

```
yn_agent/
├── bot/
│   ├── main.py
│   ├── agents/
│   │   ├── keyword_analyst.py
│   │   ├── content_editor.py
│   │   └── data_assistant.py
├── keyword_analyst/
├── content_editor/
├── data_assistant/
├── _shared/
└── ...
```

## 2. Discord 채널 + .env

최소 채널:

| 채널 | .env 키 |
|------|---------|
| `#서연우-키워드큐` | `DISCORD_CHANNEL_KEYWORD_QUEUE` |
| `#컴플라이언스-검토` | `DISCORD_CHANNEL_COMPLIANCE_REVIEW` |
| `#콘텐츠-검수대기` | `DISCORD_CHANNEL_HUMAN_REVIEW` |
| `#발행대기` | `DISCORD_CHANNEL_PUBLISH` |
| `#오지민-트래킹상태` | `DISCORD_CHANNEL_TRACKING_STATUS` |
| `#전체-긴급알림` | `DISCORD_CHANNEL_BROADCAST` |
| `#개발팀-에러알림` | `DISCORD_CHANNEL_DEV_TEAM` |

`~/.hermes/.env` 예시: `bot/.env.example` 참고.

## 3. 실행

```bash
cd yn_agent
source venv/bin/activate   # 또는 기존 환경
pip install -r bot/requirements.txt
python -m bot.main
```

## 4. 즉시 테스트 (슬래시커맨드)

| 커맨드 | 직원 | 결과 |
|--------|------|------|
| `/run_keyword_scan` | 서연우 | 키워드큐 / 컴플라이언스 |
| `/run_content_draft` | 한도윤 | 검수대기 / 발행대기 |
| `/run_anomaly_check` | 오지민 | 시나리오별 알림 |

### 오지민 시나리오

- `critical` — `#전체-긴급알림` (+ `#개발팀-에러알림`), DB `tracking_status=Critical`
- `warning` — `#오지민-트래킹상태`만
- `normal` — Discord 게시 없음 (DB만 갱신)
- `privacy` — `#개발팀-에러알림` (privacy_flags)

키워드 큐에서 **승인**하면 한도윤 초안이 자동으로 이어집니다.

## 5. DB 확인

```bash
sqlite3 bot/hermes.db "SELECT id, agent_id, routed_to FROM agent_memory ORDER BY id DESC LIMIT 15;"
sqlite3 bot/hermes.db "SELECT status, updated_at, record_id FROM tracking_status;"
```

## 문제 해결

- 채널에 안 올라옴 → `[WARN]` 로그 / 채널 ID / 봇 권한
- 슬래시커맨드 안 보임 → Discord 재시작
- `Privileged message content intent` 경고 → 시범 단계(슬래시·버튼)에서는 무시 가능
