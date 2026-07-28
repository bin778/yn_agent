"""
데모/개발 단계에서 GA4/Naver/Google Ads API 대신 쓰는 가짜 데이터.
실제 API 연동 시 이 함수 내부만 교체하면 되고, 나머지 코드는 그대로 동작한다.

의도적으로 compliance_risk=high로 판정되어야 하는 항목(3번)을 하나 넣어뒀다.
이건 "안전장치가 실제로 작동하는지" 보여주는 시연 장면 2번용이다.

오지민용 시나리오:
  critical — submit_form 0건 (Critical + broadcast 시연)
  warning  — 특정 랜딩만 -60% (Warning, 브로드캐스트 없음)
  normal   — 정상
  privacy  — 미마스킹 IP 필드 포함 (privacy_flags → 개발팀)
"""
import copy
import random

TRACKING_SCENARIOS = ("critical", "warning", "normal", "privacy")


def get_fake_search_data() -> list[dict]:
    samples = [
        {"term": "경찰조사 통보 대응 방법", "volume": 320, "source": "GA4"},
        {"term": "음주운전 2진아웃 처벌기준", "volume": 210, "source": "Naver"},
        {"term": "긴급체포 48시간 대응", "volume": 95, "source": "Google Ads"},
        # 아래 항목은 지역+사건유형 결합으로 특정 가능성이 있어 compliance_risk=high가 나와야 정상
        {"term": "강남역 성폭행 사건 가해자 변호사", "volume": 40, "source": "GA4"},
        {"term": "기소유예 이의신청 절차", "volume": 150, "source": "Naver"},
        {"term": "합의금 시세 폭행 사건", "volume": 180, "source": "Google Ads"},
    ]
    return random.sample(samples, k=len(samples))


def get_fake_tracking_data(scenario: str = "critical") -> dict:
    """오지민용 GA4/GTM 가짜 스냅샷. scenario로 시연 장면을 고른다."""
    scenario = (scenario or "critical").lower().strip()
    if scenario not in TRACKING_SCENARIOS:
        scenario = "critical"

    base = {
        "scenario": scenario,
        "ga4": {
            "sessions_today": 1200,
            "sessions_wow_avg": 1150,
            "events": {
                "click_kakaotalk": {"today": 48, "wow_avg": 50, "change_pct": -4},
                "submit_form": {"today": 36, "wow_avg": 38, "change_pct": -5},
            },
            "by_landing_page": [
                {"page": "/police-investigation", "conv_rate_today": 0.031, "conv_rate_wow": 0.033},
                {"page": "/dui-guide", "conv_rate_today": 0.028, "conv_rate_wow": 0.029},
            ],
        },
        "gtm": {
            "container_id": "GTM-DEMO",
            "last_publish_at": "2026-07-27T10:00:00+09:00",
            "publish_status": "ok",
        },
        "raw_fields_present": [],
    }

    if scenario == "critical":
        base["ga4"]["events"]["submit_form"] = {"today": 0, "wow_avg": 38, "change_pct": -100}
        base["gtm"]["publish_status"] = "suspect_tag_error"
        base["gtm"]["note"] = "트래픽 정상인데 submit_form만 급락 — GTM 태그 오류 가능성"
    elif scenario == "warning":
        base["ga4"]["by_landing_page"][1] = {
            "page": "/dui-guide",
            "conv_rate_today": 0.011,
            "conv_rate_wow": 0.029,
            "change_pct": -62,
        }
        base["gtm"]["note"] = "특정 랜딩만 국소 하락"
    elif scenario == "privacy":
        # mask_pii 단계에서 제거되어야 하는 미마스킹 필드 (시연용)
        base["raw_fields_present"] = ["client_ip", "device_id"]
        base["sample_rows_UNMASKED_DO_NOT_OUTPUT"] = [
            {"client_ip": "203.0.113.42", "device_id": "abc-device-001", "event": "page_view"},
        ]
        base["gtm"]["note"] = "ETL 마스킹 누락 의심 샘플 포함"
    # normal: base 그대로

    return copy.deepcopy(base)
