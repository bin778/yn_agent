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
