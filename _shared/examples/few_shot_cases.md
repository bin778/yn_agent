# 경계 사례 모음

> AI 산출물의 분류와 Phase 1 역할별 승인 요구를 함께 예시한다.

## 서연우 — 키워드

- `경찰조사 통보 대응 방법`: `low`, `keyword_approver` 1명 승인 후 콘텐츠 handoff.
- `OO동 특정 사건 가해자 변호사`: `high`, `keyword_approver + legal_compliance` 서로 다른 2명 승인.
- `미성년자 학교폭력 가해 대응`: `high`, 자동 콘텐츠 handoff 금지.

## 한도윤 — 콘텐츠

- 일반 절차 안내, flag 없음: `content_approver` 1명 승인 후 `publish_queue` handoff.
- `저희와 함께하면 대부분 무죄`: `outcome_guarantee` flag, `content_approver + legal_compliance` 승인.
- 확인 불가능한 판례·통계: flag를 남기고 법무 추가 승인.

## 정하준 — 퍼포먼스

- 입찰 `+15%`: `performance_approver` 1명 승인.
- 입찰 `-35%` 또는 `PAUSE`: `performance_approver + operations_owner` 승인.
- 민감 키워드 `+10%`: `performance_approver + legal_compliance` 승인.
- 민감 키워드 `PAUSE`: 서로 다른 `performance_approver + operations_owner + legal_compliance` 3명 승인.
- 모든 승인 완료 후에도 `performance_execution_pending`으로만 이동하며 광고 API를 호출하지 않는다.

## 오지민 — 데이터

- `Normal`: 상태만 기록하고 확인을 요구하지 않는다.
- `Warning/Critical`: `data_owner` 확인을 요구한다.
- privacy flag: `privacy_officer` 확인을 추가한다.
- 확인을 눌러도 퍼포먼스 차단이 자동 해제되지 않는다.
