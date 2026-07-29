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
| **서연우** | 키워드 분석가   | 위기에 처한 사람이 검색할 법한 표현을 찾되, 부당하게 표적화하지 않는 선을 지킨다 |
| **한도윤** | 콘텐츠 에디터   | 겁주지 않고 신뢰를 주는 법률 콘텐츠를 쓰되, 스스로 발행을 승인하지 않는다        |
| **정하준** | 퍼포먼스 마케터 | 데이터로만 입찰을 제안하고, 큰 폭 조정은 반드시 사람 승인을 거친다               |
| **오지민** | 데이터 분석가   | 트래킹 이상을 가장 먼저 발견해서 전체 시스템을 지킨다                            |

## 폴더 구조

```
yn_agent/
├── README.md                  ← 지금 보고 있는 문서
├── ORCHESTRATION.md            ← 4명이 어떻게 맞물리는지 전체 흐름
├── bot/                        ← Discord 파일럿 봇 (4명 실행)
│   └── SETUP.md                ← 시연 스크립트·채널·권한 체크리스트
├── _shared/                    ← 4명 공통 참조 자료
│   ├── knowledge/
│   │   ├── compliance_rules.md
│   │   └── domain_glossary.md
│   ├── examples/
│   │   └── few_shot_cases.md
│   ├── queues.md               # 큐 ↔ Discord 채널 레지스트리
│   └── roles.md                # 에스컬레이션 Discord role 정의
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
3. **시연·로컬 실행**: [bot/SETUP.md](./bot/SETUP.md)
4. 관심 있는 직원의 `SOUL.md` → `config.md` → `routine.yaml`
5. `_shared/knowledge/`, `_shared/examples/` — 공통 판단 근거

## 핵심 설계 원칙 (전체 공통)

- **판단과 실행의 분리**: AI는 분석·제안·초안까지만 자율적으로 하고, 발행/집행은 항상 사람이 최종 승인한다.
- **모르면 모른다고 한다**: 확신 없는 법률 사실, 통계, 판례는 절대 만들어내지 않고 flagged로 표시한다.
- **보수적 기본값**: 애매하면 리스크를 낮게 잡지 않고 높게 잡아 사람 검토로 보낸다.
- **감사 가능성**: 모든 직원의 모든 판단은 `audit_log`에 기록되어 나중에 추적 가능해야 한다.

## 현재 진행 상태

- [x] 4명 SOUL.md / config.md / routine.yaml 완결
- [x] 공통 knowledge (컴플라이언스 규정, 도메인 용어집) 완결
- [x] 공통 examples (경계 사례) 완결
- [x] \_shared/queues.md, \_shared/roles.md
- [x] Discord 파일럿 봇 (`bot/`) — 서연우·한도윤·오지민·정하준 + 슬래시커맨드/스케줄/승인 버튼
- [x] 시연 스크립트·채널 권한 체크리스트 ([bot/SETUP.md](./bot/SETUP.md))
- [ ] 직원별 tools.md, evals/test_cases.md
- [ ] 실데이터 API 연동 (현재 `USE_FAKE_DATA=true`)
- [ ] 승인 후 실제 실행(CMS 발행 / Ads 반영)
- [ ] Discord role ID 채우기 (`_shared/roles.md` — 서버에서 역할 생성 후)

## 변경 관리

- SOUL.md / config.md / routine.yaml 수정 시, 관련된 `_shared/knowledge`, `_shared/examples`도 함께 검토합니다.
- 변협 광고규정 등 외부 법령·규정이 개정되면 `_shared/knowledge/compliance_rules.md`를 최우선으로 갱신합니다.
