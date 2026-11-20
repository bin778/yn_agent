# Phase 3 Slack 테스트 기반 운영 계약

## 경계

Phase 3 Slack은 고정된 합성 fixture를 보여주는 UI 테스트 계층이다. Slack에서 누른
버튼과 제출한 모달은 `slack_test_sessions`, `slack_interaction_events`에만 기록한다.
`approval_requests`, `approval_decisions`, `acknowledgements`, `handoff_jobs`,
`execution_requests`를 생성하거나 변경하지 않으며 Codex CLI도 호출하지 않는다.

모든 메시지는 `PHASE 3 TEST — 실제 승인 아님`으로 표시한다. SQLite 테스트 세션이
상태 원본이며 Slack 메시지는 현재 상태를 표시하는 인터페이스다.

## Slack App 준비

1. 별도 Slack 개발 워크스페이스를 만든다.
2. Slack App 관리 화면에서 `slack/manifest.yaml`을 사용해 `Hermes Dev`를 만든다.
3. 워크스페이스에 App을 설치하고 `xoxb-` Bot User OAuth Token을 발급받는다.
4. Basic Information에서 `connections:write` scope의 `xapp-` App-Level Token을 만든다.
5. 비공개 `#hermes-dev` 채널을 만들고 Hermes Bot을 초대한다.
6. `slack/mapping.example.yaml`을 `~/.hermes/slack_mapping.yaml`로 복사하고 실제
   Workspace, User, Channel ID로 바꾼다. Phase 3의 다섯 queue는 같은 채널이어도 된다.
7. `~/.hermes/.env`에 아래 값을 추가한다.

```ini
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
SLACK_MAPPING_PATH=~/.hermes/slack_mapping.yaml
SLACK_TEST_SESSION_TTL_MINUTES=15
```

토큰과 실제 매핑 파일은 저장소, DB, 로그에 복사하지 않는다. Bot scope는
`commands`, `chat:write`만 사용하며 Events API 구독과 공개 HTTP endpoint는 만들지 않는다.

## 로컬 실행과 직접 테스트

```bash
python -m pip install -r bot/requirements.txt
python scripts/validate_phase0.py --gate
python scripts/validate_phase1.py --gate
python scripts/validate_phase2.py --gate
python scripts/validate_codex_provider.py --gate
python scripts/validate_phase3.py --gate
python -m unittest discover -v

# 아래 두 명령은 Slack 설정 후 실행한다.
python scripts/validate_phase3.py --live-gate
python -m bot.slack_app
```

`--live-gate`는 `auth.test`로 Workspace를 확인하고 `#hermes-dev`에 삭제하지 않는
테스트 카드 한 건을 게시한다. 그 후 Socket Mode 프로세스를 실행한 상태에서 Slack에서
다음을 순서대로 입력한다.

```text
/hermes-test status
/hermes-test keyword
/hermes-test content
/hermes-test performance
/hermes-test data
```

- `keyword`는 단일 역할 fixture라 한 명의 테스트 승인으로 완료할 수 있다.
- 나머지 복수 역할 fixture는 첫 역할 처리 뒤 `pending`을 유지한다.
- 동일 Slack 사용자가 두 번째 역할까지 충족할 수 없는 것이 정상이다.
- 반려·수정 요청은 사유가 없는 경우 모달을 제출할 수 없어야 한다.
- 15분이 지난 버튼과 종료된 카드의 버튼은 stale 응답이어야 한다.

## 장애 처리

- `xoxb-`/`xapp-` 누락 또는 Workspace 불일치: 프로세스는 시작하지 않는다.
- 사용자·채널·역할·queue 매핑 오류: 해당 명령이나 액션을 거부한다.
- Slack 재전송: 안정적인 payload 필드로 만든 idempotency key를 재사용해 한 번만 기록한다.
- Slack 연결 장애: Phase 1 승인 및 외부 실행 상태는 변하지 않는다. 연결 복구 후 테스트를
  다시 실행한다.
- 로그에는 토큰, 전체 Slack payload, 모달 사유 원문을 기록하지 않는다.

## Phase 4 인계

Phase 4에서는 현재 identity provider, Block Kit renderer, idempotency, message binding을
유지하고 테스트 세션 서비스 대신 실제 work item 조회와 승인 서비스를 연결한다. Phase 3
테이블은 운영 감사 기록으로 승격하지 않는다.

### Phase 4 개발 검증

일반 합성 키워드 한 건을 Phase 1 승인 엔진에 연결하는 개발 명령을 별도로 켤 수
있습니다. 개발 워크스페이스의 기존 앱과 `/hermes-test` 등록을 사용합니다.
이미 실행 중인 Socket Mode 프로세스를 종료한 뒤 다음과 같이 재시작합니다.

```bash
HERMES_DB_PATH="$HOME/.hermes/hermes-dev.db" \
  python -m bot.slack_app --approval-demo-db "$HOME/.hermes/hermes-approval-demo.db"
```

1. 매핑의 개발 채널에서 `keyword_approver` 역할로 `/hermes-test approval-demo`를 실행합니다.
2. **키워드 승인**을 누릅니다. 카드가 승인 완료로 바뀌고 별도 데모 DB에 워크아이템,
   승인 결정과 감사 이벤트, `draft_content` 대기열 작업이 기록됩니다.
3. 새 카드에서 반려 또는 수정 요청을 선택하면 사유가 필요하며 초안 작업은 생성되지 않습니다.
4. Slack 카드 갱신에 실패하면 카드에 표시된 승인 요청 ID로
   `/hermes-test approval-demo <요청-ID>`를 실행해 현재 상태를 다시 표시합니다.

이 명령은 합성 데이터를 사용하며 초안 작업을 대기열에 넣는 데까지 진행합니다.
콘텐츠 워커, 모델 호출, CMS 발행, 광고 실행은 여기서 시작하지 않습니다.

#### 콘텐츠 승인 카드

같은 개발 DB에서 합성 키워드의 `draft_content` 작업을 처리한 후, 생성된 일반
콘텐츠 승인 요청 ID를 사용합니다. 위의 `--approval-demo-db` 옵션으로 봇을
재시작한 뒤 `#hermes-dev`에서 다음 명령을 실행합니다.

```text
/hermes-test content-review <콘텐츠 승인 요청 ID>
```

`content_approver`는 제목, 본문, 위험 플래그, 만료 시각을 검토하고 승인·반려·수정
요청을 할 수 있습니다. 같은 명령은 기존 카드의 상태를 다시 표시합니다. 승인하면
`queue_for_publish`가 개발 DB에 한 건 등록됩니다. 일반 합성 초안을 대상으로 하며,
다중 역할 승인과 발행 실행은 별도 단계입니다.

### Hermes에서 콘텐츠 초안 요청

1. 운영용 Hermes 앱에 `slack/manifest.prod.yaml`을 적용합니다. 추가된 `commands`
   권한을 승인하도록 Yeoon 워크스페이스에 앱을 다시 설치합니다.
2. EC2 서비스 환경 파일에서 `SLACK_DRAFT_CHANNEL_ID`를 Hermes가 참여한 비공개
   채널의 ID로 지정합니다. 이 채널에서만 요청과 승인 버튼을 처리합니다.
3. `hermes` 계정에서 설정된 `CODEX_COMMAND`를 실행할 수 있고, 같은 계정의
   `CODEX_HOME`에 CLI 인증이 준비되어 있어야 합니다. `hermes-slack`을 재시작한
   뒤 로그에서 `draft_requests=True`와 Socket Mode 연결을 확인합니다.
4. 채널에서 `/hermes-draft 첫 상담 전 준비할 질문`을 실행합니다.
   `keyword_approver`, `content_approver`, `workflow_admin` 역할 중 하나가 필요합니다.
   요청자가 접수 안내를 받은 뒤 같은 채널에 콘텐츠 승인 카드가 올라옵니다.
   일반 초안은 `content_approver`가 승인하고, 위험 플래그가 있는 초안은
   별도 계정의 `legal_compliance` 승인도 필요합니다.
5. 승인 대기 중인 카드에서 콘텐츠 승인자가 **초안 수정**을 누르면 제목·도입부·본문을
   편집할 수 있습니다. 저장하면 위험 항목 검토 후 같은 카드가 새 버전의 승인
   대기로 돌아갑니다. 만료 시각은 카드에 한국 시간으로 표시됩니다.

개인 스테이징에서 혼자 승인 흐름을 검증하려면 `/etc/hermes/hermes.env`에
`SLACK_CONTENT_APPROVAL_MODE=single`을 넣고 서비스를 재시작합니다.
`APP_ENV=staging`, `EXECUTION_ENABLED=false`, `EXECUTION_MODE=disabled` 상태에서 사용할
수 있습니다. 새 초안은 위험 플래그를 표시하면서 콘텐츠 승인자 1명의 승인을 받습니다.
기존 요청과 해당 초안의 수정본은 처음 저장한 승인 모드를 따릅니다.
운영 기본값인 `risk_based`에서는 위험 플래그가 있는 초안에 서로 다른 두 계정의 승인이
필요합니다.

요청, 초안, 승인 결과는 `HERMES_DB_PATH`에 기록됩니다. 카드 게시가 실패하면
워커가 저장된 초안으로 다시 시도합니다. 승인 완료 후 발행 검토 대기열에 등록되며,
dry-run 검증은 같은 DB에서 카드의 콘텐츠 승인 요청 ID를 지정해 실행할 수 있습니다.
