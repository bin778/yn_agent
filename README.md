# Hermes AI — 법무법인 여온 마케팅 AI 직원 시스템

## 이게 뭔가요

법무법인 여온 마케팅팀을 위한 4명의 자율 AI 직원 정의 저장소입니다.
각 직원은 고유한 역할·판단 원칙·권한 경계를 가지고, 스스로 스케줄에 따라 일하며,
서로에게 작업을 넘기고(handoff), 판단이 애매하거나 리스크가 있으면 사람에게 넘깁니다(escalation).

**중요**: 이 저장소는 "AI가 알아서 다 한다"는 시스템이 아닙니다.
**판단은 AI가 자율적으로 하되, 실행(발행/광고집행/예산반영)은 반드시 사람 승인을 거칩니다.**
자세한 흐름은 [ORCHESTRATION.md](./ORCHESTRATION.md)를 참고하세요.

## 4명의 AI 직원

| 이름       | 역할            | 한 줄 정의                                                                       |
| ---------- | --------------- | -------------------------------------------------------------------------------- |
| **윤서진** | 키워드 분석가   | 위기에 처한 사람이 검색할 법한 표현을 찾되, 부당하게 표적화하지 않는 선을 지킨다 |
| **한도윤** | 콘텐츠 에디터   | 겁주지 않고 신뢰를 주는 법률 콘텐츠를 쓰되, 스스로 발행을 승인하지 않는다        |
| **김도현** | 퍼포먼스 마케터 | 데이터로만 입찰을 제안하고, 큰 폭 조정은 반드시 사람 승인을 거친다               |
| **임지혁** | 데이터 분석가   | 트래킹 이상을 가장 먼저 발견해서 전체 시스템을 지킨다                            |

## 폴더 구조

```
yn_agent/
├── README.md                  ← 지금 보고 있는 문서
├── ORCHESTRATION.md            ← 4명이 어떻게 맞물리는지 전체 흐름
├── bot/                        ← 승인 엔진·실행 안전 경계·Slack 테스트 어댑터·4명 실행 로직
├── _shared/                    ← 4명 공통 참조 자료
│   ├── knowledge/
│   │   ├── compliance_rules.md
│   │   └── domain_glossary.md
│   ├── examples/
│   │   └── few_shot_cases.md
│   ├── queues.md               # 플랫폼 독립 논리 큐 정의
│   └── roles.md                # Human `role_key` 정의
├── docs/phase0/                ← 플랫폼 전환 전 워크플로·외부 실행 계약
├── docs/phase1/                ← 역할별 승인 워크플로 계약
├── docs/phase2/                ← 실행 안전장치·dry-run 계약
├── docs/phase2_5/              ← ChatGPT 로그인 기반 Codex CLI 모델 공급자
├── docs/phase3/                ← Slack Socket Mode 테스트 UI 운영 계약
├── slack/                      ← Slack App manifest와 비밀값 없는 매핑 예제
├── evals/                      ← 코드 fixture, 법률 Golden Set 후보, 경계 사례와 평가 기준
├── scripts/validate_phase0.py  ← 자동 검증 및 다음 Phase 진입 Gate
└── {agent_name}/               # 직원별 폴더 (동일 구조 반복)
    ├── SOUL.md
    ├── config.md
    ├── routine.yaml
    ├── tools.md                # (예정)
    └── evals/test_cases.md     # (예정)
```

## 이 문서들을 처음 보시는 분께 (읽는 순서 추천)

1. 이 README.md (지금 이 문서)
2. [ORCHESTRATION.md](./ORCHESTRATION.md) — 전체 그림
3. 관심 있는 직원의 `SOUL.md` → `config.md` → `routine.yaml`
4. `_shared/knowledge/`, `_shared/examples/` — 공통 판단 근거

## 핵심 설계 원칙 (전체 공통)

- **판단과 실행의 분리**: AI는 분석·제안·초안까지만 자율적으로 하고, 발행/집행은 항상 사람이 최종 승인한다.
- **모르면 모른다고 한다**: 확신 없는 법률 사실, 통계, 판례는 절대 만들어내지 않고 flagged로 표시한다.
- **보수적 기본값**: 애매하면 리스크를 낮게 잡지 않고 높게 잡아 사람 검토로 보낸다.
- **감사 가능성**: 모든 직원의 모든 판단은 `audit_events`에 append-only로 기록되어 나중에 추적 가능해야 한다.

## 현재 진행 상태

- [x] 4명 SOUL.md / config.md / routine.yaml 완결
- [x] 공통 knowledge (컴플라이언스 규정, 도메인 용어집) 완결
- [x] 공통 examples (경계 사례) 완결
- [x] \_shared/queues.md, \_shared/roles.md
- [x] Phase 1 역할별 승인 엔진 — 이력형 결정, 버전, TTL, idempotent handoff
- [x] Phase 2 실행 안전장치 — default deny, kill switch, allowlist, 한도, 멱등성, dry-run
- [x] Phase 2.5 모델 전환 — Gemini 제거, 로그인 기반 Codex CLI 구조화 출력
- [x] Phase 3 Slack 테스트 구현 — Socket Mode, `/hermes-test`, 버튼·모달, 테스트 세션
- [ ] Phase 3 개발 워크스페이스 연결 및 Live Gate
- [x] Phase 0 Engineering Gate — 구현 기준선·평가 후보·근거 PDF 무결성 검증
- [ ] Release Human Gate — Slack 검토 화면 이후 제품·법률·개인정보·보안 담당 승인
- [ ] 직원별 tools.md, evals/test_cases.md
- [ ] 실데이터 API 연동 (현재 `USE_FAKE_DATA=true`)
- [ ] 승인 후 실제 실행(CMS 발행 / Ads 반영)
- [ ] Phase 4 Slack 파일럿 — 실제 work item 표시와 승인 엔진 연결

## 변경 관리

- SOUL.md / config.md / routine.yaml 수정 시, 관련된 `_shared/knowledge`, `_shared/examples`도 함께 검토합니다.
- 변협 광고규정 등 외부 법령·규정이 개정되면 `_shared/knowledge/compliance_rules.md`를 최우선으로 갱신합니다.

## Phase 0 검증

자동 구조·근거·개인정보 패턴 검증:

```bash
python scripts/validate_phase0.py
python -m unittest tests.test_phase0_artifacts
```

Phase 1 진입 Engineering Gate:

```bash
python scripts/validate_phase0.py --gate
```

법률 Golden Set과 정책 문서의 최종 사람 검수는 Slack 최소 구현물 확인 후 진행합니다.
제한 파일럿 또는 운영 전에는 아래 Release Gate를 반드시 통과해야 합니다.

```bash
python scripts/validate_phase0.py --release-gate
```

## Phase 1 검증

역할별 승인, 다중 승인, 버전, 만료, 데이터 확인, handoff와 기존 플랫폼 제거를 검증합니다.

```bash
python scripts/validate_phase1.py --gate
python -m unittest discover -v
```

## Phase 2 검증

승인·최신 버전·정책 버전·allowlist·변경 한도·tracking 상태를 확인한 뒤에만
네트워크 전송 없는 dry-run을 허용합니다. 현재 실제 외부 쓰기 adapter는 없습니다.

```bash
python scripts/validate_phase2.py --gate
python -m unittest discover -v
```

세부 계약은 [docs/phase2/execution_safety.md](./docs/phase2/execution_safety.md)를 참고하세요.

## Phase 2.5 검증

현재 macOS 사용자의 ChatGPT 로그인으로 Codex CLI를 호출한다. API 키나 OAuth 토큰은
프로젝트 환경 파일에 저장하지 않는다.

```bash
codex login status
python scripts/validate_codex_provider.py
python scripts/validate_codex_provider.py --gate
python -m unittest discover -v
```

`--gate`는 실제 구조화 출력 1회를 실행하므로 소량의 구독 사용량을 소비한다. 세부
운영 계약은 [docs/phase2_5/codex_cli_provider.md](./docs/phase2_5/codex_cli_provider.md)를
참고하세요.

## Phase 3 검증

Phase 3는 별도 개발 워크스페이스의 Socket Mode에서 고정 합성 fixture와 테스트 전용
SQLite 세션만 사용합니다. Slack 버튼은 실제 승인, handoff, Codex 또는 외부 실행을
발생시키지 않습니다.

```bash
python scripts/validate_phase3.py --gate
python -m unittest discover -v

# ~/.hermes/.env와 ~/.hermes/slack_mapping.yaml 구성 후 실행
python scripts/validate_phase3.py --live-gate
python -m bot.slack_app
```

Slack에서 `/hermes-test status`를 실행한 뒤 `keyword`, `content`, `performance`, `data`
테스트 카드를 확인합니다. 설정 및 직접 테스트 절차는
[docs/phase3/slack_test_foundation.md](./docs/phase3/slack_test_foundation.md)를 참고하세요.


## Slack 팀 브리핑

`/hermes-team brief`는 하나의 스레드에 콘텐츠 제작팀, 데이터 분석팀, 퍼포먼스 마케팅팀의 결과를 모읍니다. Slack 앱에 운영 매니페스트의 명령을 등록하고 `SLACK_TEAM_BRIEFS_ENABLED=true`로 설정하면 `SLACK_DRAFT_CHANNEL_ID` 채널에서 사용할 수 있습니다.

- 기본 요청은 합성 정상 데이터를 사용합니다. `/hermes-team brief critical`은 트래킹 이상 시나리오를 사용합니다. 팀 점검은 운영 트래킹 상태를 갱신하지 않습니다.
- `HERMES_TEAM_KEYWORD_APPROVAL_MODE=legal_before_draft`가 기본값입니다. 고위험 키워드는 서로 다른 키워드 승인자와 법무 승인자가 승인하면 초안을 작성합니다.
- 1인 운영에서는 `HERMES_TEAM_KEYWORD_APPROVAL_MODE=draft_advisory`를 선택할 수 있습니다. 고위험 카드에는 법무 검토 권고와 일반화한 초안 주제가 표시됩니다. 키워드 승인 한 번으로 검토용 초안을 작성하며 고위험 키워드에서 파생된 초안의 발행 승인에는 콘텐츠·법무 두 역할이 필요합니다. 팀 브리핑에는 검토용 초안이 표시됩니다.
- 모드는 브리핑 요청 시 저장됩니다. 설정 변경은 신규 요청에 적용됩니다. PM 카드는 키워드·법무 승인 대기, 권고, 반려, 초안 상태와 팀별 결과를 구분합니다. 다음 행동에서 승인 후보, 검토용 초안, 전환 추적 점검 등 해당 Slack 답글을 바로 열 수 있습니다.
- 퍼포먼스 담당은 같은 브리핑의 트래킹 점검을 사용합니다. Critical, 개인정보 신호 또는 점검 실패 시 광고 제안을 보류합니다.
- 팀 요청과 단계 실행 상태는 SQLite에 저장됩니다. Slack 전송에 실패해도 저장된 분석을 다시 실행하지 않습니다.

로컬 검증은 `python -m unittest tests.test_slack_team -v`로 실행합니다.
