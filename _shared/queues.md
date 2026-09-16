# Hermes 논리 큐/상태 레지스트리

> 큐 키는 채팅 플랫폼과 무관하며 DB가 상태의 진실의 원천이다.
> Phase 3 Slack 테스트에서는 별도 매핑 파일이 논리 큐를 개발 채널에 연결한다.
> 실제 승인 및 handoff 라우팅 연결은 Phase 4 범위다.

| `queue_key`                      | 의미                                | 소비 조건                                             |
| -------------------------------- | ----------------------------------- | ----------------------------------------------------- |
| `content_editor.inbox`           | 승인된 키워드의 콘텐츠 작성 handoff | `draft_content` handoff를 원자적으로 claim            |
| `compliance_review.queue`        | 법률/민감 조건이 추가된 승인 요청   | 검증된 `legal_compliance`                             |
| `human_review.queue`             | flagged/sensitive 콘텐츠 검토       | `content_approver` + `legal_compliance`               |
| `publish_queue`                  | 승인된 콘텐츠의 내부 발행 대기      | `queue_for_publish` handoff 완료; 외부 게시 권한 없음 |
| `performance_review.queue`       | 모든 입찰/예산 제안 검토            | `performance_approver` 및 조건부 추가 역할            |
| `performance_execution_pending`  | 승인된 제안의 실행 검토 대기        | 외부 실행은 Phase 2 정책으로 계속 차단                |
| `data_assistant.tracking_status` | Normal/Warning/Critical 최신 상태   | 퍼포먼스 AI는 실행 전 조회                            |
| `broadcast.all_agents`           | Critical 전파                       | 알림이며 승인이 아님                                  |
| `dev_team.queue`                 | 시스템·트래킹·개인정보 오류         | 담당자 조치 필요                                      |
| `pattern_alert.queue`            | 반복 패턴 경고                      | 개별 승인 없음                                        |

## 불변 규칙

1. 승인 요청은 큐 메시지가 아닌 `approval_requests`에 저장한다.
2. 승인 완료 전에는 handoff job을 생성하지 않는다.
3. handoff는 `(work_item_id, version, handoff_type)`당 한 번만 생성한다.
4. `publish_queue`와 `performance_execution_pending`은 내부 대기열이며 외부 실행을 의미하지 않는다.
