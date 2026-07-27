# 서연우 파일럿 — 로컬 실행 가이드

## 1. 폴더 배치
이 `bot/` 폴더를 `yn_agent/` 루트 바로 아래에 놓으세요.

```
yn_agent/
├── bot/                  ← 이 폴더
│   ├── main.py
│   ├── ...
├── keyword_analyst/
├── _shared/
└── ...
```

## 2. 사전 준비
- [ ] DISCORD_SETUP_CHECKLIST.md 진행 (최소한 `#서연우-키워드큐`, `#컴플라이언스-검토` 두 채널은 있어야 함)
- [ ] Google AI Studio에서 `GOOGLE_API_KEY` 발급 (https://aistudio.google.com/apikey)
- [ ] `~/.hermes/.env`에 아래 값 채우기 (bot/.env.example 참고)

```
DISCORD_BOT_TOKEN=...
GOOGLE_API_KEY=...
DISCORD_CHANNEL_KEYWORD_QUEUE=<채널 우클릭 → ID 복사>
DISCORD_CHANNEL_COMPLIANCE_REVIEW=<채널 우클릭 → ID 복사>
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
[Hermes] Yeoon#1234 로그인 완료. 서버: ['여온 마케팅']
[Hermes] 스케줄러 시작됨 (서연우: 매일 09:00 KST)
```

Discord에서도 봇이 "온라인"으로 표시됩니다.

## 4. 즉시 테스트 (매일 09:00까지 기다릴 필요 없음)

Discord 채널에서 `/run_keyword_scan` 슬래시커맨드를 입력하면 즉시 실행됩니다.
이게 바로 이전에 논의한 **시연용 트리거**입니다.

정상 작동하면:
- `#서연우-키워드큐`에 일반 키워드들이 embed + 승인/반려 버튼과 함께 게시됨
- `#컴플라이언스-검토`에 "강남역 성폭행 사건 가해자 변호사" 항목이 별도로 게시됨 (fake_data.py에 의도적으로 넣어둔 high-risk 테스트 항목)

이 두 번째 동작이 바로 이전에 말씀드린 **"시연 장면 2 — 안전장치 작동"**입니다.

## 5. 결과 확인 (DB)

```bash
sqlite3 bot/hermes.db "SELECT id, agent_id, routed_to, human_decision FROM agent_memory ORDER BY id DESC LIMIT 10;"
```

Discord에서 승인/반려 버튼을 누르면 `human_decision` 칼럼에 반영되는지 확인할 수 있습니다.

## 문제 해결
- `필수 환경변수가 없습니다` → `.env` 위치와 키 이름 확인
- 채널에 아무것도 안 올라옴 → 콘솔의 `[WARN]` 로그 확인 (채널ID 오류 또는 봇 권한 문제일 가능성 높음)
- 슬래시커맨드가 안 보임 → Discord 클라이언트 재시작 (동기화 반영에 시간이 걸릴 수 있음)
- Gemini 응답 파싱 실패가 반복됨 → `GEMINI_MODEL` 값이 유효한 모델명인지 확인
