"""
yn_agent/{agent_id}/SOUL.md, config.md, routine.yaml 및
yn_agent/_shared/ 의 공통 문서를 읽어서 시스템 프롬프트를 조립한다.

핵심 원칙: 문서(SOUL.md 등)가 진실의 원천(source of truth)이고,
코드는 그걸 그대로 읽어서 프롬프트로 조립할 뿐, 판단 로직을 하드코딩하지 않는다.
"""
import yaml
from pathlib import Path
from .config import BASE_DIR


def _read(path: Path) -> str:
    if not path.exists():
        print(f"[WARN] 파일 없음: {path}")
        return ""
    return path.read_text(encoding="utf-8")


def load_agent_docs(agent_id: str):
    agent_dir = BASE_DIR / agent_id
    soul = _read(agent_dir / "SOUL.md")
    config_doc = _read(agent_dir / "config.md")
    routine_raw = _read(agent_dir / "routine.yaml")
    routine = yaml.safe_load(routine_raw) if routine_raw else {}
    return soul, config_doc, routine


def load_shared_knowledge():
    shared = BASE_DIR / "_shared"
    compliance = _read(shared / "knowledge" / "compliance_rules.md")
    glossary = _read(shared / "knowledge" / "domain_glossary.md")
    examples = _read(shared / "examples" / "few_shot_cases.md")
    return compliance, glossary, examples


def build_system_prompt(agent_id: str) -> str:
    """
    SOUL.md + config.md + 공통 knowledge/examples 를 합쳐서
    이 직원의 시스템 프롬프트를 만든다.
    """
    soul, config_doc, _ = load_agent_docs(agent_id)
    compliance, glossary, examples = load_shared_knowledge()

    return f"""당신은 아래 정의된 정체성과 규칙에 따라 판단하는 AI 직원입니다.
반드시 아래 모든 원칙과 경계를 지키며, 지시된 JSON 스키마로만 응답하십시오.

# 정체성 및 판단 원칙
{soul}

# 설정 및 권한 경계
{config_doc}

# 컴플라이언스 규정 요약 (반드시 준수)
{compliance}

# 도메인 용어집
{glossary}

# 판단 경계 사례 (아래 사례의 판단 기준을 따를 것)
{examples}
"""
