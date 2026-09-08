# Phase 1 역할별 승인 워크플로

상태: `implemented`

## 상태

`approval_requests.status`는 `pending`, `approved`, `rejected`, `revision_requested`, `expired`, `cancelled`만 사용한다. `pending`을 제외한 상태는 해당 작업 버전의 최종 상태다.

- 일반 키워드/콘텐츠: 7일 후 만료
- 고위험 키워드/콘텐츠: 3일 후 만료
- 퍼포먼스 제안: 24시간 후 만료

## 승인 정책

| 정책 | 필수 역할 |
| --- | --- |
| `keyword_standard` | `keyword_approver` |
| `keyword_high` | `keyword_approver`, `legal_compliance` |
| `content_standard` | `content_approver` |
| `content_legal` | `content_approver`, `legal_compliance` |
| 일반 퍼포먼스 | `performance_approver` |
| 큰 조정/PAUSE | `performance_approver`, `operations_owner` |
| 민감 퍼포먼스 | 기본 역할 + `legal_compliance` |

필수 역할이 여러 개면 승인자도 모두 달라야 한다. 반려와 수정 요청은 사유가 필수다. 수정된 산출물은 기존 버전을 덮어쓰지 않고 `version + 1`로 다시 승인받는다.

## 데이터 확인

Normal은 기록만 남긴다. Warning/Critical은 `data_owner`, privacy flag는 `privacy_officer` 확인을 요구한다. 확인은 읽음 기록이며 승인, 외부 실행, Critical 차단 해제가 아니다.

## 안전 조건

- 검증된 신원과 역할이 없으면 결정을 거부한다.
- 승인 완료 전에는 handoff를 생성하지 않는다.
- 동일 사용자는 한 요청에서 한 역할로만 결정할 수 있다.
- 종료된 상태·이전 버전·만료된 요청은 stale action으로 거부한다.
- 결정과 감사 이벤트는 append-only로 저장한다.
- 승인 완료는 외부 실행 허가가 아니다.
