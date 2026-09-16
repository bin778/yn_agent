# Phase 2.5 — Codex CLI 모델 공급자

## 범위

Phase 2.5는 Gemini API 호출을 제거하고, 로컬 macOS 사용자가 ChatGPT로 로그인한
Codex CLI를 Hermes의 구조화 출력 공급자로 사용한다. Slack UI, AWS·Docker 인증,
외부 게시와 광고 변경은 이 단계의 범위가 아니다.

## 로컬 준비

Codex CLI 로그인과 인증 방식을 확인한다.

```bash
codex login
codex login status
```

상태 출력은 `Logged in using ChatGPT`여야 한다. API 키 로그인, 로그아웃, 실행 파일
미설치 상태에서는 Hermes가 fail-closed로 LLM 작업을 거부한다.

```dotenv
LLM_PROVIDER=codex_cli
CODEX_COMMAND=codex
CODEX_TIMEOUT_SECONDS=180
CODEX_MAX_CONCURRENCY=1
```

`OPENAI_API_KEY`, `CODEX_API_KEY`, `CODEX_ACCESS_TOKEN`은 이 로컬 구독 경로에서
사용하지 않는다. `~/.codex/auth.json`은 비밀번호처럼 취급하며 저장소, 환경 파일,
DB 또는 로그에 복사하지 않는다.

## 실행 경계

- 프롬프트는 shell 인수가 아니라 표준입력으로 전달한다.
- 하위 프로세스 환경은 HOME·PATH·임시 경로·인증서 경로만 허용하고 앱 토큰은 상속하지 않는다.
- 실행마다 빈 임시 디렉터리와 Pydantic 기반 JSON Schema를 만든다.
- `--ephemeral`, `--sandbox read-only`, `--ignore-user-config`, `--ignore-rules`를 고정한다.
- Codex는 저장소를 작업 디렉터리로 받지 않으며 파일 수정 권한을 받지 않는다.
- stdout의 최종 JSON만 검증하고 stderr 및 프롬프트 원문을 감사 로그에 남기지 않는다.
- 스키마 오류만 최대 2회 재시도한다. 인증, 실행, timeout 오류는 자동 재시도하지 않는다.
- 구독 한도 또는 인증 오류는 담당자가 확인한 뒤 수동으로 재실행한다.

## 운영 점검

정적 검사만 실행하려면:

```bash
python scripts/validate_codex_provider.py
```

로그인 및 실제 구조화 출력 1회를 포함한 Phase 3 진입 Gate:

```bash
python scripts/validate_codex_provider.py --gate
```

Gate는 소량의 ChatGPT 구독 사용량을 소비한다. 실패 시 `codex login status`를 먼저
확인하고, 필요하면 `codex login`으로 다시 로그인한다. 모델은 CLI 기본값을 사용하며
작업에는 `codex-cli/<CLI 버전>:default` 형식으로 기록한다.
