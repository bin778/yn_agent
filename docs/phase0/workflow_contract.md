# Hermes Phase 0 워크플로 계약

상태: `implementation_baseline`

이 문서는 채팅 플랫폼과 LLM 공급자에 무관하게 Hermes가 지켜야 하는 업무 계약을 정의한다.
채팅 플랫폼이나 LLM 공급자 구현은 이 계약을 변경하지 않는다.

## 1. 공통 불변 조건

1. AI의 산출물은 분석, 제안 또는 초안이며 사람의 승인이나 외부 실행이 아니다.
2. 승인 전에는 승인을 요구하는 다음 단계로 handoff하지 않는다.
3. 반려된 작업은 같은 승인 버전에서 다시 승인하거나 실행할 수 없다.
4. 같은 작업과 같은 승인 유형의 결정은 한 번만 확정할 수 있다.
5. 권한을 확인할 수 없으면 승인 또는 실행을 거부한다(fail-closed).
6. 생성, 라우팅, 승인, 반려, handoff, 실행 시도와 결과는 각각 감사 이벤트로 기록한다.
7. 채팅 메시지는 사용자 인터페이스일 뿐 업무 상태의 진실의 원천이 아니다.
8. 법률 근거나 사실을 확인할 수 없으면 생성하지 않고 사람 검토로 보낸다.

## 2. 공통 상태

```text
pending
  -> approved | rejected | revision_requested | expired | cancelled
```

- `approved`, `rejected`, `revision_requested`, `expired`, `cancelled`는 동일한 작업 버전에서 최종 상태다.
- 재검토가 필요하면 기존 결정을 덮어쓰지 않고 새 작업 버전을 만든다.
- handoff 소비자는 `(work_item_id, work_item_version, handoff_type)` idempotency key로 한 번만 처리한다.

## 3. 직원별 계약

### 3.1 서연우 — 키워드 분석가

- 입력: 검색/광고 데이터와 최근 검토 이력
- 출력: 키워드, 검색 의도, urgency, compliance risk, 판단 사유
- 모든 키워드는 `keyword_approver` 승인을 받는다.
- `compliance_risk=high`면 서로 다른 `legal_compliance` 승인을 추가한다.
- 필수 승인 완료 후에만 `draft_content` handoff를 생성한다.

### 3.2 한도윤 — 콘텐츠 에디터

- 입력: 승인된 키워드 작업 ID와 키워드 데이터
- 출력: 콘텐츠 초안, flagged claims, sensitive flags, 판단 사유
- 모든 초안은 `content_approver` 승인을 받는다.
- flagged/sensitive가 하나라도 있으면 서로 다른 `legal_compliance` 승인을 추가한다.
- 승인 후 `publish_queue`로 handoff하지만 CMS 발행을 의미하지 않는다.

### 3.3 정하준 — 퍼포먼스 마케터

- 입력: 신뢰 가능한 집계 광고 데이터와 최신 tracking status
- tracking status가 `Critical`이면 제안을 생성하지 않는다.
- 모든 제안은 `performance_approver` 승인을 받는다.
- 조정폭 절댓값이 20을 초과하거나 `PAUSE`이면 `operations_owner` 승인을 추가한다.
- 민감 카테고리는 조정폭과 무관하게 `legal_compliance` 승인이 추가로 필요하다.
- 필요한 모든 승인이 끝나도 결과는 실행 대기 상태이며 광고 API를 호출하지 않는다.

### 3.4 오지민 — 데이터 분석가

- 입력: PII가 제거된 집계 데이터
- `Normal`: 상태만 갱신하고 일반 채널에는 게시하지 않는다.
- `Warning`: `data_owner` 확인을 요구한다.
- `Critical`: `data_owner` 확인을 요구하고 광고 제안을 보류한다.
- privacy flag가 있으면 `privacy_officer` 확인을 추가한다. 확인은 차단 해제가 아니다.
- 식별 가능한 원문이 발견되면 모델 입력 전에 제거하고 개발팀 큐로 보낸다.

## 4. 승인 유형과 완료 조건

| 작업                  | 승인 유형                                                | 완료 조건                      |
| --------------------- | -------------------------------------------------------- | ------------------------------ |
| 일반 키워드 -> 콘텐츠 | `keyword_approver` | 키워드 담당자 1명 |
| 고위험 키워드 | `keyword_approver` + `legal_compliance` | 서로 다른 2명 |
| 일반 콘텐츠 | `content_approver` | 콘텐츠 담당자 1명 |
| flagged/sensitive 콘텐츠 | `content_approver` + `legal_compliance` | 서로 다른 2명 |
| 일반 퍼포먼스 제안 | `performance_approver` | 퍼포먼스 담당자 1명 |
| 큰 폭/Pause 광고 | `performance_approver` + `operations_owner` | 서로 다른 2명 |
| 민감 광고 | 기본 역할 + `legal_compliance` | 요구된 모든 역할 승인 |

승인 완료는 외부 실행 허가의 필요조건일 뿐 충분조건이 아니다. 외부 실행 조건은
[`execution_policy.md`](./execution_policy.md)를 따른다.

## 5. 감사 이벤트 최소 필드

```text
event_id
event_type
work_item_id
work_item_version
agent_id
actor_type
actor_id
platform
model_provider
model_id
prompt_version
occurred_at
previous_state
new_state
reason
correlation_id
```

## 6. 변경 통제

- 이 계약의 승인 임계치나 라우팅 변경은 새 문서 버전과 평가 케이스를 함께 추가한다.
- 기존 감사 이벤트와 승인 이벤트는 수정하지 않는다.
- 이 계약은 Phase 1 구현 기준선이다. 법률 의미와 사례 라벨의 최종 승인은 Slack 검토 화면이 준비된 뒤 Release Gate에서 확정한다.
