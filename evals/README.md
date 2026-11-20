# Hermes 평가 데이터셋

이 디렉터리는 프롬프트 예제가 아니라 회귀 테스트와 모델 비교를 위한 평가 데이터다.
모델이 생성한 라벨을 정답으로 채택하지 않으며 법률 Golden Set은 담당자의 검수를 거친다.

## 구성

- `fixtures/`: 코드, 라우팅, 상태 전이를 검증하는 합성 사례
- `legal_golden/`: 법률/컴플라이언스 담당자가 승인해야 하는 후보 사례
- `adversarial/`: 우회 표현, 특정 가능성, 프롬프트 인젝션 등 경계 사례
- `holdout/`: 프롬프트에 노출하지 않는 모델 비교용 사례 운영 지침
- `rubric.yaml`: 평가 기준과 Phase 0 Gate

## 데이터 원칙

1. 실상담 기록과 실제 사건 원문을 저장하지 않는다.
2. 공개 규정 기반 합성 사례 또는 재식별할 수 없는 복합 사례를 우선한다.
3. 모든 사례는 `contains_personal_data`와 `provenance`를 표시한다.
4. 법률 판단 사례는 `_shared/knowledge/sources.yaml`의 근거 ID를 참조한다.
5. 규정이 바뀌면 관련 source ID를 참조하는 모든 사례를 재검토한다.
6. `review.status=approved`는 지정된 사람 검수자만 설정한다.

## 검증

자동 구조 검증:

```bash
python scripts/validate_phase0.py
python -m unittest tests.test_phase0_artifacts
```

Phase 1 진입 Engineering Gate:

```bash
python scripts/validate_phase0.py --gate
```

`--gate`는 근거 PDF 무결성, 후보 데이터 표시, 워크플로와 외부 실행 정책을 검사한다.
통과하면 안전한 핵심 워크플로 구현을 시작할 수 있다.

Slack 검토 화면 이후 운영 Release Gate:

```bash
python scripts/validate_phase0.py --release-gate
```

`--release-gate`는 법률/개인정보/실행정책 사람 검수 상태가 모두 승인되기 전에는
실패해야 정상이다. 후보 라벨은 Slack에서 실제 모델 결과와 승인 흐름을 확인한 뒤 확정한다.

## Slack 콘텐츠 프롬프트 회귀 평가

[프롬프트 평가 사례](content_prompt/cases.yaml)는 요청의 초점, 비교 범위,
근거 확인 대상, 근거가 불충분한 주장과 입력에 섞인 지시를 점검한다.
사례는 합성 입력이며 기대 항목은 응답 구조와 검토 기준이다.

저장소 루트에서 Hermes에 사용하는 Python 환경으로 실행한다.

\`\`\`bash
python -m scripts.eval_content_prompt --list
python -m scripts.eval_content_prompt --case CONTENT-01 --case CONTENT-02 > /tmp/content-eval.jsonl
\`\`\`

\`--case\`를 생략하면 모든 사례를 실행한다. 출력 한 줄에는 원래 질문, 점검 기준,
모델 식별자와 실제 생성한 초안이 들어간다. 실행은 임시 DB를 사용한다.
프롬프트 전후의 결과를 같은 모델에서 비교하고, 각 \`must\`와 \`must_not\` 충족 여부를
사람이 판정한다. 근거 없는 법률 단정과 입력에 섞인 게시 지시 수용은 실패로 기록한다.
