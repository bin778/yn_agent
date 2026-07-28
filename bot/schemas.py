"""
직원별 출력 JSON 스키마.
config.md/routine.yaml의 output_schema 이름과 1:1 대응한다.
"""
from typing import List, Literal
from pydantic import BaseModel, Field


class KeywordItem(BaseModel):
    keyword: str
    estimated_search_volume: int
    intent_type: str = Field(description="Information, Comparison, Action 등")
    urgency_level: int = Field(ge=1, le=5)
    compliance_risk: Literal["low", "medium", "high"]
    compliance_note: str = Field(default="", description="medium/high일 경우 사유")
    reason: str


class KeywordAnalystOutput(BaseModel):
    items: List[KeywordItem]


class ContentEditorOutput(BaseModel):
    """한도윤(content_editor) 초안 출력. routine.yaml content_editor_schema."""

    target_keyword: str
    title: str
    hook: str = Field(description="담백한 도입부 1~2문장")
    body: str = Field(description="본문 초안 (마크다운 가능)")
    flagged_claims: List[str] = Field(
        default_factory=list,
        description="결과 보장·긴급성 조장·미검증 판례 등 자기점검 플래그. 없으면 빈 배열",
    )
    sensitive_flags: List[str] = Field(
        default_factory=list,
        description="형량/합의금 구체 언급, 미성년자·피해자 관점, 신규 판례·법개정 등. 없으면 빈 배열",
    )
    reason: str = Field(description="플래그/톤 선택에 대한 짧은 근거")
