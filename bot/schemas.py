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


class DataAssistantOutput(BaseModel):
    """오지민(data_assistant) 이상탐지 출력. routine.yaml data_assistant_schema."""

    status_summary: Literal["Normal", "Warning", "Critical"]
    tracking_anomalies: List[str] = Field(
        default_factory=list,
        description="관찰된 이상 목록 (집계 단위로만 서술, 개인 특정 금지)",
    )
    action_items: List[str] = Field(
        default_factory=list,
        description="실행 가능한 다음 조치",
    )
    privacy_flags: List[str] = Field(
        default_factory=list,
        description="미마스킹 PII 등. 없으면 빈 배열. 원문 IP/기기ID는 절대 포함하지 말 것",
    )
    suspected_layer: str = Field(
        default="",
        description="원인 후보 단계: GTM / ETL / GA4 / unknown 등",
    )
    reason: str = Field(description="사실 → 판단 → 다음 행동 요약")


class BidRecommendation(BaseModel):
    """정하준 입찰/예산 조정 제안 1건."""

    keyword: str
    campaign: str = Field(default="", description="캠페인명")
    action: Literal["INCREASE", "DECREASE", "PAUSE", "HOLD"]
    adjustment_percentage: int = Field(
        description="입찰 조정 비율. PAUSE/HOLD면 0. 예: 15 = +15%, -35 = -35%"
    )
    target_device: str = Field(default="all", description="mobile / desktop / all")
    target_hour_range: str = Field(default="", description="예: 00:00-06:00, 비어있으면 전체")
    category: str = Field(default="", description="DUI, 폭행, 성범죄, 미성년자 등")
    sensitive_keyword: bool = Field(
        default=False,
        description="성범죄·미성년자 등 민감 키워드면 true → compliance_review 병행",
    )
    caution_flag: bool = Field(
        default=False,
        description="트래킹 Warning 등으로 신중 진행 시 true",
    )
    rationale: str = Field(description="관찰된 지표만 근거로 서술. 심리 상태 언급 금지")


class PerformanceAnalystOutput(BaseModel):
    """정하준(performance_analyst) 출력. routine.yaml performance_analyst_schema."""

    recommendations: List[BidRecommendation]
