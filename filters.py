"""
PASS 필터 (룰북 §4-1, §7-1)
진입 가능 여부를 결정하는 7가지 안전 조건.
모두 PASS여야 진입 후보로 분류.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

import config
from indicators import avg_lower_wick_ratio


@dataclass
class FilterResult:
    """필터 통과 여부 + 사유."""
    passed: bool
    name: str
    detail: str = ""

    def __str__(self) -> str:
        icon = "✓" if self.passed else "✗"
        return f"{icon} {self.name}: {self.detail}"


@dataclass
class FilterReport:
    """전체 필터 결과."""
    symbol: str
    results: list[FilterResult]

    @property
    def all_passed(self) -> bool:
        return all(r.passed for r in self.results)

    @property
    def passed_count(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def total_count(self) -> int:
        return len(self.results)

    @property
    def failed(self) -> list[FilterResult]:
        return [r for r in self.results if not r.passed]


# ============================================================
# 개별 필터
# ============================================================
def filter_ath(price: float, ath: float) -> FilterResult:
    """현재가 < ATH * 0.95 (신고가 영역 회피)."""
    if ath <= 0 or price <= 0:
        return FilterResult(False, "ATH 필터", "ATH 데이터 없음")
    ratio = price / ath
    passed = ratio < config.ATH_BUFFER
    return FilterResult(
        passed,
        "ATH 필터",
        f"price/ATH={ratio:.3f} ({'< 0.95 OK' if passed else '≥ 0.95 신고가 영역'})",
    )


def filter_recent_high(price: float, recent_max: float) -> FilterResult:
    """직전 30일 고점 미돌파."""
    if recent_max <= 0 or price <= 0:
        return FilterResult(False, "30일고점 필터", "데이터 없음")
    passed = price < recent_max
    return FilterResult(
        passed,
        "30일고점 필터",
        f"price={price:.6g}, 30d_high={recent_max:.6g} ({'미돌파 OK' if passed else '돌파!'})",
    )


def filter_wick(daily_df: pd.DataFrame) -> FilterResult:
    """
    일봉 아래꼬리 비율 < 30% (계단식 상승 = 자전거래 회피).
    룰북 원문은 3D/주봉이지만 일봉도 같은 원리.
    """
    if daily_df is None or len(daily_df) < config.WICK_LOOKBACK_BARS:
        return FilterResult(False, "아래꼬리 필터", "캔들 부족")
    ratio = avg_lower_wick_ratio(daily_df, config.WICK_LOOKBACK_BARS)
    passed = ratio < config.WICK_RATIO_MAX
    return FilterResult(
        passed,
        "아래꼬리 필터",
        f"최근 {config.WICK_LOOKBACK_BARS}봉 평균={ratio*100:.1f}% "
        f"({'< 30% OK' if passed else '≥ 30% 계단식 상승'})",
    )


def filter_funding(funding_info: dict | None) -> FilterResult:
    """
    펀딩비 함정 회피.
    음펀비가 너무 셀 때 = 숏 진영이 펀비 내는 상황 = 위험.
    바이낸스는 기본 8h 주기, 일부 알트는 1h/4h.
    """
    if not funding_info:
        return FilterResult(False, "펀비 필터", "데이터 없음")
    rate = float(funding_info.get("rate", 0) or 0)
    raw_interval = funding_info.get("interval_hours", 8) or 8
    try:
        interval = float(raw_interval)
    except (TypeError, ValueError):
        interval = 8.0
    if interval <= 0:
        interval = 8.0

    # 1시간 주기로 환산 → 룰북의 1H 펀비 기준과 비교
    hourly_equiv = rate / interval
    # 그리고 8h 표준 주기 기준 절대값으로도 비교
    passed_1h = hourly_equiv > config.FUNDING_1H_MIN
    passed_8h = rate > config.FUNDING_8H_MIN if interval >= 8 else True

    passed = passed_1h and passed_8h
    return FilterResult(
        passed,
        "펀비 필터",
        f"rate={rate*100:.4f}% per {interval}h, 1h환산={hourly_equiv*100:.4f}% "
        f"({'OK' if passed else '음펀비 위험'})",
    )


def filter_bb_deviation(deviation_pctl: float) -> FilterResult:
    """
    BB 이격도가 종목 30일 분포 95퍼센타일 이상.
    룰북 §3-2 굿쇼존 진입 조건과 동일.
    """
    threshold = config.GOOD_SHORT_PCTL  # 90 또는 사용자 설정
    passed = deviation_pctl >= threshold
    return FilterResult(
        passed,
        "이격도 필터",
        f"BB이격 백분위={deviation_pctl:.1f}% "
        f"({'≥ ' + str(threshold) + '% OK' if passed else '< ' + str(threshold) + '% 이격 부족'})",
    )


def filter_abs_bb_deviation(deviation: float) -> FilterResult:
    """
    절대 BB 이격이 너무 작으면 차단.
    백분위는 높아도 절대값이 작으면 익절 거리가 짧아
    펀비/수수료 뗐을 때 EV가 무너지는 케이스 회피.
    deviation = (price - bb_upper) / bb_upper
    """
    threshold = config.MIN_ABS_BB_DEVIATION
    passed = deviation >= threshold
    return FilterResult(
        passed,
        "절대이격 필터",
        f"abs_dev={deviation*100:+.2f}% "
        f"({'≥ ' + f'{threshold*100:.1f}%' + ' OK' if passed else '< ' + f'{threshold*100:.1f}%' + ' 익절거리 부족'})",
    )


def filter_liq_cluster(distance_to_upper_cluster: float | None) -> FilterResult:
    """
    위쪽 청산 클러스터까지 거리 ≥ 3%.
    Coinglass 등 연동 필요 (현재는 데이터 없으면 PASS로 간주).
    """
    if distance_to_upper_cluster is None:
        return FilterResult(
            True,  # 데이터 없으면 보수적으로 PASS (사용자가 별도 확인)
            "청산맵 필터",
            "Coinglass 미연동 (수동 확인 권장)",
        )
    passed = distance_to_upper_cluster >= config.LIQ_CLUSTER_MIN_DIST
    return FilterResult(
        passed,
        "청산맵 필터",
        f"위쪽 클러스터 거리={distance_to_upper_cluster*100:.2f}% "
        f"({'≥ 3% OK' if passed else '< 3% 위험'})",
    )


def filter_symbol_blacklist(symbol_meta: dict) -> FilterResult:
    """
    상장폐지 임박 종목 (바이낸스의 ST 표시 등) 회피.
    ccxt에서는 status로 일부 감지 가능.
    """
    if symbol_meta.get("contractType") == "DELIVERING":
        return FilterResult(False, "ST 필터", "DELIVERING 상태")
    info = symbol_meta.get("info", {})
    if isinstance(info, dict) and info.get("status") in ("BREAK", "SETTLING"):
        return FilterResult(False, "ST 필터", f"비활성 상태: {info.get('status')}")
    return FilterResult(True, "ST 필터", "정상 거래중")


# ============================================================
# Deadking 블랙리스트 (운영자가 'Deadking zone' 으로 발사한 종목 = 단타 단발, 반복 금지)
# ============================================================
def is_deadking_blacklisted(symbol: str) -> bool:
    """심볼이 DEADKING_BLACKLIST 에 포함되어 있나 — base ticker 기준."""
    base = symbol.split("/", 1)[0].upper()
    bl = getattr(config, "DEADKING_BLACKLIST", []) or []
    return base in {b.upper() for b in bl}


def is_operator_traded(symbol: str) -> bool:
    """심볼이 OPERATOR_TRADED_SYMBOLS 에 포함되어 있나 — base ticker 기준.
    강의 채널에서 운영자가 실제 비중 진입/정리한 자리.
    """
    base = symbol.split("/", 1)[0].upper()
    tl = getattr(config, "OPERATOR_TRADED_SYMBOLS", []) or []
    return base in {b.upper() for b in tl}


def filter_deadking_blacklist(symbol: str) -> FilterResult:
    """
    운영자가 Deadking zone 으로 발사한 종목은 자동 NONE 처리.
    Deadking zone 은 단타 단발성 — 같은 종목 반복 진입은 손실 누적.
    config.DEADKING_BLACKLIST 에서 관리하며, 신규 발사 시 사용자가 수동 추가.
    """
    base = symbol.split("/", 1)[0].upper()
    if is_deadking_blacklisted(symbol):
        return FilterResult(
            False,
            "Deadking 블랙리스트",
            f"{base} 차단 (운영자 Deadking zone 종목, 단타 단발 → 반복 진입 금지)",
        )
    return FilterResult(True, "Deadking 블랙리스트", f"{base} 정상")


# ============================================================
# 통합 필터 실행
# ============================================================
def run_all_filters(
    symbol: str,
    price: float,
    ath: float,
    recent_max: float,
    daily_df: pd.DataFrame,
    funding_info: dict | None,
    deviation_pctl: float,
    deviation_abs: float,
    distance_to_upper_cluster: float | None = None,
    symbol_meta: dict | None = None,
) -> FilterReport:
    """8가지 PASS 조건 모두 실행 (7 룰북 + 1 절대이격)."""
    results = [
        filter_ath(price, ath),
        filter_recent_high(price, recent_max),
        filter_wick(daily_df),
        filter_funding(funding_info),
        filter_bb_deviation(deviation_pctl),
        filter_abs_bb_deviation(deviation_abs),
        filter_liq_cluster(distance_to_upper_cluster),
        filter_symbol_blacklist(symbol_meta or {}),
    ]
    return FilterReport(symbol=symbol, results=results)
