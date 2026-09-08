# Hermes 외부 실행 정책

상태: `implementation_baseline`

## 1. 기본 정책

Hermes의 외부 실행은 기본적으로 거부한다(default deny).

```dotenv
EXECUTION_ENABLED=false
SCHEDULER_ENABLED=false
```

AI 생성, 사람 승인, 외부 실행은 서로 다른 상태와 감사 이벤트다. 사람의 승인은
외부 시스템 호출을 자동으로 의미하지 않는다.

## 2. 외부 실행의 범위

다음은 모두 외부 실행이다.

- CMS 게시, 수정, 삭제
- Google Ads/Naver 광고 입찰, 예산, 캠페인 상태 변경
- 고객 또는 사건 관계자에게 메시지 발송
- 외부 공개 채널 게시
- 원본 분석 데이터의 수정 또는 삭제

내부 DB 기록과 내부 검토 큐 게시는 외부 업무 실행이 아니지만 최소 권한과 감사 기록을 적용한다.

## 3. 실행 활성화 필요조건

외부 실행 서비스는 아래 조건을 모두 만족할 때만 요청을 처리한다.

1. `APP_ENV=production`이고 `EXECUTION_ENABLED=true`다.
2. 작업에 요구된 승인 유형이 모두 승인되어 있다.
3. 승인 대상 작업 버전과 실행 대상 버전이 일치한다.
4. 실행자는 허용된 서비스 계정이며 최소 권한만 가진다.
5. 대상 시스템, 작업 종류, 금액/변경폭이 allowlist와 한도 안에 있다.
6. `(work_item_id, execution_type)` idempotency key가 이전에 성공하지 않았다.
7. 최신 tracking status와 compliance policy version이 유효하다.
8. 비상 중지(kill switch)가 활성화되어 있지 않다.

조건을 확인할 수 없거나 일부가 누락되면 실행을 거부하고 감사 이벤트를 남긴다.

## 4. 단계적 권한 부여

```text
READ_ONLY
  -> DRY_RUN
      -> CANARY
          -> LIMITED_PRODUCTION
```

- `READ_ONLY`: 외부 데이터 조회만 허용
- `DRY_RUN`: 실행 요청 payload를 만들지만 전송하지 않음
- `CANARY`: 승인된 제한 대상과 작은 한도에서만 실행
- `LIMITED_PRODUCTION`: allowlist 안에서만 운영 실행

각 단계 승격은 테스트 결과와 사람 승인을 별도 기록한다.

## 5. 비밀과 네트워크 경계

- 분석 에이전트에는 CMS/Ads 쓰기 자격증명을 제공하지 않는다.
- 실행 자격증명은 별도 실행 서비스만 읽을 수 있다.
- 로그, 프롬프트, 채팅 메시지에 토큰이나 원본 개인정보를 기록하지 않는다.
- 외부 목적지는 allowlist로 제한한다.

## 6. 실패와 롤백

- 실행 API timeout은 성공 여부 불명 상태로 기록하고 idempotency 조회 전 재시도하지 않는다.
- 실행 결과의 외부 객체 ID와 응답 상태를 감사 로그에 남긴다.
- 삭제보다 비활성화, 되돌리기 요청 등 복구 가능한 동작을 우선한다.
- 예산/입찰 변경에는 변경 전 값을 저장해 보상 작업을 가능하게 한다.

## 7. Gate 구분

- Phase 0 Engineering Gate: 외부 실행 기본 거부와 단계적 권한 부여가 명문화되어야 한다.
- Release Gate: 보안 담당자와 업무 책임자의 검토 상태가 모두 `approved`여야 한다.
- 법률 또는 개인정보 관련 실행은 법률/개인정보 담당자의 추가 승인을 받아야 한다.
