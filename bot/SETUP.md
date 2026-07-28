# 서연우 + 한도윤 파일럿 — 로컬 실행 가이드

## 1. 폴더 배치
이 `bot/` 폴더를 `yn_agent/` 루트 바로 아래에 놓으세요.

```
yn_agent/
├── bot/                  ← 이 폴더
│   ├── main.py
│   ├── agents/
│   │   ├── keyword_analyst.py
│   │   └── content_editor.py
│   ├── ...
├── keyword_analyst/
├── content_editor/
├── _shared/
└── ...
```

## 2. 사전 준비
- [ ] Discord에 최소 4개 채널: `#서연우-키워드큐`, `#컴플라이언스-검토`, `#콘텐츠-검수대기`, `#발행대기`
- [ ] Google AI Studio에서 `GOOGLE_API_KEY` 발급 (https://aistudio.google.com/apikey)
- [ ] `~/.hermes/.env`에 아래 값 채우기 (bot/.env.example 참고)

```
DISCORD_BOT_TOKEN=...
GOOGLE_API_KEY=...
DISCORD_CHANNEL_KEYWORD_QUEUE=<채널 ID>
DISCORD_CHANNEL_COMPLIANCE_REVIEW=<채널 ID>
DISCORD_CHANNEL_HUMAN_REVIEW=<채널 ID>
DISCORD_CHANNEL_PUBLISH=<채널 ID>
```

> Discord에서 채널ID를 복사하려면: 설정 → 고급 → 개발자 모드 ON 후 채널 우클릭 → "ID 복사"

## 3. 설치 및 실행

```bash
cd yn_agent
python3 -m venv venv
source venv/bin/activate          # Windows는 venv\Scripts\activate
pip install -r bot/requirements.txt

python -m bot.main
```

정상이면 터미널에 이렇게 뜹니다.

```
[Hermes] 슬래시커맨드 동기화 완료
[Hermes] Yeoon#1234 로그인 완료. 서버: ['Yeoon Agent']
[Hermes] 스케줄러 시작됨 (서연우 09:00 / 한도윤 백로그 10:00·15:00 KST)
```

## 4. 즉시 테스트

### 서연우
Discord에서 `/run_keyword_scan` →
- `#서연우-키워드큐`에 일반 키워드 + 승인/반려
- `#컴플라이언스-검토`에 high-risk 항목

### 한도윤 (핸드오프)
`#서연우-키워드큐`에서 **✅ 승인**을 누르면:
1. DB에 승인 기록
2. 한도윤이 초안 작성
3. flagged/sensitive 있으면 `#콘텐츠-검수대기`, 없으면 `#발행대기`에 게시

이미 예전에 승인만 해둔 키워드가 있으면 `/run_content_draft`로 백로그를 처리할 수 있습니다.

## 5. 결과 확인 (DB)

```bash
sqlite3 bot/hermes.db "SELECT id, agent_id, routed_to, human_decision FROM agent_memory ORDER BY id DESC LIMIT 15;"
```

## 문제 해결
- `필수 환경변수가 없습니다` → `.env` 위치와 키 이름 확인
- 채널에 아무것도 안 올라옴 → 콘솔의 `[WARN]` 로그 확인 (채널ID 오류 또는 봇 권한 문제)
- 슬래시커맨드가 안 보임 → Discord 클라이언트 재시작
- Gemini 응답 파싱 실패가 반복됨 → `GEMINI_MODEL` 값이 유효한 모델명인지 확인
- 승인 후 한도윤이 안 움직임 → `DISCORD_CHANNEL_HUMAN_REVIEW` / `DISCORD_CHANNEL_PUBLISH` 설정 여부 확인
