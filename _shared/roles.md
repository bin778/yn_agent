# 에스컬레이션 대상 정의 (Discord role 매핑 포함)

> routine.yaml의 `escalation.notify` 필드는 이 문서에 정의된 role_key를 참조한다.
> Discord role ID는 아직 미정 — 서버에 role 생성 후 채우면 된다.

## 사용법

1. Discord 서버 설정 → 역할(Role) 메뉴에서 아래 `discord_role_name` 기준으로 역할 생성
2. 실제 담당자를 해당 역할에 배정
3. 역할 우클릭 → ID 복사 → `discord_role_id`에 채움
4. bot 코드는 `notify: ['marketing_lead']` 같은 routine.yaml 값을 이 표에서 찾아 실제 멘션으로 변환

---

## 역할 목록

### `marketing_lead` — 마케팅팀장

- **담당 업무**: 광고 조정 최종 승인, 반복 문제 패턴 발생 시 1차 판단
- **관련 큐**: `human_approval.queue`, `compliance_review.queue`(공동), 각종 반복 패턴 알림
- **discord_role_name**: `@마케팅팀장`
- **discord_role_id**: (미정)
- **실제 담당자**: (이름 기입)
- **연락 우선순위**: 즉시 (업무시간 내)

### `legal_compliance` — 법무팀 컴플라이언스 담당

- **담당 업무**: compliance_risk=high 키워드 검토, 콘텐츠 최종 규정 판정, 규정 개정 반영
- **관련 큐**: `compliance_review.queue`, `human_review.queue`(공동)
- **discord_role_name**: `@법무컴플라이언스`
- **discord_role_id**: (미정)
- **실제 담당자**: (이름 기입)
- **연락 우선순위**: 24시간 내 (긴급 아닌 경우), Critical 연동 시 즉시

### `attorney_reviewer` — 콘텐츠 검수 변호사

- **담당 업무**: 한도윤 초안 중 flagged_claims 있는 콘텐츠의 법률 사실 검증
- **관련 큐**: `human_review.queue`
- **discord_role_name**: `@콘텐츠검수변호사`
- **discord_role_id**: (미정)
- **실제 담당자**: (이름 기입, 로테이션이면 로테이션 방식도 기입)
- **연락 우선순위**: 발행 전 필수, 급하지 않으면 24시간 내

### `dev_team` — 개발팀

- **담당 업무**: 시스템 오류, 파싱 실패, API 무응답, GTM 태그 오류 대응
- **관련 큐**: `dev_team.queue`, `broadcast.all_agents`(공동 수신)
- **discord_role_name**: `@개발팀`
- **discord_role_id**: (미정)
- **실제 담당자**: (이름 기입)
- **연락 우선순위**: Critical은 즉시, 나머지는 업무시간 내

### `publish_operator` — 발행 담당자

- **담당 업무**: `publish_queue` 최종 확인 후 실제 CMS/웹사이트 발행 실행
- **관련 큐**: `publish_queue`
- **discord_role_name**: `@발행담당자`
- **discord_role_id**: (미정)
- **실제 담당자**: (이름 기입)
- **연락 우선순위**: 급하지 않음, 정기 확인으로 충분

---

## 알림 우선순위 기준 (참고)

| 우선순위       | 의미                                         | 채널 표기 관행              |
| -------------- | -------------------------------------------- | --------------------------- |
| 즉시(Critical) | 트래킹 완전 중단, 시스템 오류 연쇄 발생 가능 | `@here` 또는 role 전체 멘션 |
| 당일 내        | 반복 패턴, 반려율 이상 등                    | role 멘션, 조용한 알림      |
| 정기 확인      | 자동 반영 대기, 발행 대기 등 일상적 흐름     | 멘션 없이 채널 게시만       |

## 이 문서가 바뀌어야 할 때

- 담당자가 바뀔 때 (인사이동 등)
- 새로운 역할이 필요해질 때 (예: 5번째 직원 추가로 새 승인 라인 필요)
- Discord role 구조가 재편될 때
