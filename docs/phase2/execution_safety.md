# Phase 2 — 실행 안전장치와 테스트

## 범위

Phase 2는 승인된 산출물이 외부 실행 경계에 도달했을 때 안전하게 차단하거나
시뮬레이션하는 계층을 구현한다. 이 단계에는 CMS·광고·메시징 플랫폼의 실제 API
클라이언트나 운영 자격 증명이 없다.

지원 모드는 다음 세 가지뿐이다.

| 모드        | 동작                                           |
| ----------- | ---------------------------------------------- |
| `disabled`  | 모든 실행 요청 차단                            |
| `read_only` | 내장 fake adapter로 읽기 결과만 시뮬레이션     |
| `dry_run`   | 전송 없이 실행 payload의 마스킹된 preview 생성 |

`canary`, `limited_production`, 실제 쓰기 adapter는 Phase 2 범위가 아니다.

## 기본값과 환경 변수

환경 변수가 없거나 잘못되면 항상 차단한다.

```dotenv
APP_ENV=development
EXECUTION_ENABLED=false
EXECUTION_MODE=disabled
EXECUTION_KILL_SWITCH=true
EXECUTION_ALLOWED_TARGETS=
EXECUTION_MAX_BID_CHANGE_PERCENT=20
EXECUTION_MAX_BUDGET_CHANGE_PERCENT=20
EXECUTION_TRACKING_MAX_AGE_MINUTES=180
EXECUTION_ALLOW_CAMPAIGN_PAUSE=false
COMPLIANCE_POLICY_VERSION=phase0-implementation-baseline
```

Boolean 값은 `true` 또는 `false`만 허용한다. `yes`, `1`과 같은 값이나 알 수 없는
모드는 설정 오류로 처리한다.

## 요청 검증 순서

실행 요청은 아래 조건을 모두 만족해야 adapter에 전달된다.

1. 설정에 오류가 없고 `EXECUTION_ENABLED=true`이다.
2. kill switch가 꺼져 있다.
3. 검증된 서비스 identity가 `execution:request` 권한을 가진다.
4. 실행 유형이 현재 모드에 허용되어 있다.
5. Phase 2에 내장된 `DisabledAdapter`, `FakeReadOnlyAdapter`, `DryRunAdapter` 중 하나이며
   외부 네트워크를 사용하지 않는다.
6. target이 정확한 allowlist 항목과 일치한다.
7. 요청과 현재 설정의 컴플라이언스 정책 버전이 일치한다.
8. 대상 `work_item_id + version`이 최신 버전이며 상태가 `approved`이다.
9. AI 직원 유형과 실행 유형의 허용 매핑이 일치한다.
10. 요청 correlation ID가 승인된 작업의 correlation ID와 일치한다.
11. 쓰기 payload가 승인된 해당 버전의 AI 산출물과 정확히 일치한다.
12. 전달한 승인 요청이 동일 작업·버전의 완료된 승인이다.
13. 광고 요청은 설정된 유효시간 이내의 tracking 상태가 존재하고 `Critical`이 아니다.
14. 광고 실행 유형, 승인된 action, 변경률과 캠페인 중지 정책이 서로 일치하며
    설정 한도를 만족한다.

하나라도 실패하면 `blocked`로 종료하고 adapter는 호출하지 않는다.

## 멱등성과 불확실한 결과

멱등 키는 `(work_item_id, version, execution_type, target, payload_hash)`로 만든다.
동일 요청은 최초 결과만 반환하며 adapter를 다시 호출하지 않는다.

Timeout은 실패로 추측하지 않고 `unknown`으로 기록한다. 외부 시스템에서 성공했을
가능성을 배제할 수 없기 때문에 같은 멱등 키로 자동 재시도하지 않는다.

## 데이터 최소화와 감사 로그

- 원문 payload는 DB에 저장하지 않는다. 알려진 민감 key는 제거하고, 그 밖의 자유
  텍스트도 내용 대신 길이와 자료형만 저장한다.
- credential 계열 key, Bearer token, 이메일, 휴대전화번호, 주민등록번호 패턴은
  preview와 결과에서 `[REDACTED]`로 대체한다.
- payload 원문 대신 SHA-256 hash와 마스킹된 summary만 저장한다.
- `execution_requests`에는 현재 결과를, append-only `execution_attempts`에는 실행
  시도를 기록한다.
- 모든 상태 변화에는 서비스 actor, 작업 버전, 사유, correlation ID를 포함한
  `audit_events`가 남는다.

## 승인된 콘텐츠 한 건의 발행 dry-run

Slack에서 승인한 콘텐츠에 발행 검토 작업이 등록된 상태에서 개발 DB와 콘텐츠 승인
요청 ID를 지정합니다.

```bash
python -m scripts.verify_publish_dry_run \
  --db "$HOME/.hermes/hermes-approval-demo.db" \
  --approval-request-id "콘텐츠-승인-요청-ID"
```

CLI는 원본을 읽기 전용으로 열고 임시 SQLite 복사본에서만 실행 기록을 생성합니다.
정상 요청과 재전송을 확인한 뒤, 별도 복사본에서 이전 버전, 변조된 본문, 긴급 중지를
검증합니다. 결과에는 상태와 실행 시도 횟수만 표시하며 원본 대기열은 소비하지 않습니다.

## Gate

```bash
python scripts/validate_phase0.py --gate
python scripts/validate_phase1.py --gate
python scripts/validate_phase2.py --gate
python -m unittest discover -v
```

모두 통과해도 허용되는 것은 fake read-only와 dry-run뿐이다. 실제 외부 쓰기는 별도
adapter, 자격 증명 관리, canary/rollback, Release Human Gate가 구현되기 전까지 금지한다.
