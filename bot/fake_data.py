"""
데모/개발 단계에서 GA4/Naver/Google Ads API 대신 쓰는 가짜 데이터.
실제 API 연동 시 이 함수 내부만 교체하면 되고, 나머지 코드는 그대로 동작한다.

의도적으로 compliance_risk=high로 판정되어야 하는 항목(3번)을 하나 넣어뒀다.
이건 "안전장치가 실제로 작동하는지" 보여주는 시연 장면 2번용이다.
"""
import random


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
