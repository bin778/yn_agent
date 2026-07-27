"""
Gemini API 호출 래퍼 (2026년 기준 google-genai SDK 사용).
Pydantic 모델을 response_schema로 그대로 넘기면 SDK가 자동으로 파싱해준다.
스키마 검증 실패 시 config.md의 retry_on_schema_fail(2회) 규칙대로 재시도한다.
"""
from google import genai
from google.genai import types
from pydantic import BaseModel, ValidationError

from .config import GOOGLE_API_KEY, GEMINI_MODEL

_client = genai.Client(api_key=GOOGLE_API_KEY)


class SchemaParseError(Exception):
    pass


def call_gemini_structured(
    system_prompt: str,
    user_prompt: str,
    schema_model: type[BaseModel],
    max_retries: int = 2,
) -> BaseModel:
    """
    system_prompt: SOUL.md 등을 합쳐 만든 이 직원의 시스템 프롬프트
    user_prompt: 이번 작업(오늘의 데이터 등)
    schema_model: 응답을 검증할 Pydantic 모델 (예: KeywordAnalystOutput)
    """
    last_error = None
    for attempt in range(max_retries + 1):
        try:
            response = _client.models.generate_content(
                model=GEMINI_MODEL,
                contents=user_prompt,
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    response_mime_type="application/json",
                    response_schema=schema_model,
                    temperature=0.2,  # 일관된 판정이 중요한 도메인이라 낮게 설정
                ),
            )
            # SDK가 response_schema로 pydantic 모델을 넘기면 .parsed 로 바로 인스턴스를 준다.
            if response.parsed is not None:
                return response.parsed
            # 혹시 .parsed가 비어있으면 텍스트에서 직접 검증
            return schema_model.model_validate_json(response.text)

        except (ValidationError, ValueError) as e:
            last_error = e
            print(f"[서연우][재시도 {attempt + 1}/{max_retries}] 스키마 검증 실패: {e}")

    raise SchemaParseError(f"스키마 검증 {max_retries}회 재시도 후에도 실패: {last_error}")
