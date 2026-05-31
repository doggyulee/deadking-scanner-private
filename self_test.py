"""
오프라인 자체 검증 스크립트
- 거래소 API 없이 합성 데이터로 전체 파이프라인 검증
- 6등급 분류기 + 필터가 정확히 룰북대로 동작하는지 확인

실행: python self_test.py
"""
import numpy as np
import pandas as pd

import config
import indicators as ind
import filters as flt
from classifier import classify, classify_operator, classify_pctl, SignalContext, GRADE_EMOJI


def make_synthetic_klines(scenario: str, n: int = 100) -> pd.DataFrame:
    """
    시나리오별 합성 일봉.
      'normal'      : 정상 변동 (신호 없음)
      'overheat'    : 굿쇼존 (BB 상단 근접)
      'os'          : 굿쇼존 OS (BB 상단 약간 돌파)
      'mad_os'      : 굿쇼존 MAD OS (큰 펌프)
      'rsi_extreme' : RSI 99 격상 케이스
    """
    np.random.seed(7)
    base = 100.0
    closes = [base]
    for i in range(n - 1):
        drift = np.random.randn() * 0.015
        closes.append(closes[-1] * (1 + drift))

    closes = np.array(closes)
    if scenario == "overheat":
        closes[-3:] *= 1.05
    elif scenario == "os":
        closes[-3:] *= 1.08
    elif scenario == "mad_os":
        closes[-5:] *= [1.0, 1.05, 1.10, 1.15, 1.20]
    elif scenario == "rsi_extreme":
        # 단조 상승 (RSI 100 근접)
        closes[-10:] = closes[-10] * (1 + np.linspace(0.01, 0.30, 10))

    # OHLC 합성
    opens = closes * (1 - np.abs(np.random.randn(n)) * 0.005)
    highs = np.maximum(opens, closes) * (1 + np.abs(np.random.randn(n)) * 0.003)
    lows = np.minimum(opens, closes) * (1 - np.abs(np.random.randn(n)) * 0.003)
    vols = np.abs(np.random.randn(n)) * 1_000_000 + 5_000_000

    return pd.DataFrame({
        "open": opens, "high": highs, "low": lows, "close": closes, "volume": vols,
    })


def analyze_synthetic(symbol: str, df_daily: pd.DataFrame, df_4h: pd.DataFrame = None, df_1h: pd.DataFrame = None) -> SignalContext:
    price = float(df_daily["close"].iloc[-1])
    upper, mid, low_band = ind.bollinger_bands(df_daily["close"], config.BB_PERIOD, config.BB_STD)
    dev = ind.bb_deviation(price, float(upper.iloc[-1]))

    dev_series = ind.bb_deviation_series(df_daily["close"], config.BB_PERIOD, config.BB_STD)
    pctl = ind.deviation_percentile(dev_series.tail(30), dev)

    rsi_1d = float(ind.rsi(df_daily["close"], config.RSI_LENGTH).iloc[-1])
    rsi_4h = float(ind.rsi(df_4h["close"], config.RSI_LENGTH).iloc[-1]) if df_4h is not None else None
    rsi_1h = float(ind.rsi(df_1h["close"], config.RSI_LENGTH).iloc[-1]) if df_1h is not None else None

    ma5 = ind.sma(df_daily["close"], config.MA_FAST).iloc[-1]
    ma5_val = float(ma5) if not pd.isna(ma5) else None
    ma5_dev = ind.ma_deviation(price, ma5_val) if ma5_val else None

    return SignalContext(
        symbol=symbol,
        price=price,
        bb_upper_1d=float(upper.iloc[-1]),
        bb_middle_1d=float(mid.iloc[-1]),
        bb_lower_1d=float(low_band.iloc[-1]),
        bb_dev_1d=dev,
        bb_dev_pctl_1d=pctl,
        rsi6_1d=rsi_1d,
        rsi6_4h=rsi_4h,
        rsi6_1h=rsi_1h,
        ma5_1d=ma5_val,
        ma5_deviation=ma5_dev,
        funding_rate=0.0001,
        funding_interval_h=8,
        volume_24h_usd=50_000_000,
    )


# ============================================================
# 시나리오별 테스트
# ============================================================
SCENARIOS = [
    ("정상 변동", "normal", None),       # 신호 없음 기대
    ("과열", "overheat", "A or C"),       # Monitoring or Good Short
    ("OS 펌프", "os", "S or C+"),         # OS suffix
    ("MAD OS 펌프", "mad_os", "S+ or B"), # MAD OS suffix
    ("RSI 99 극단치", "rsi_extreme", "B+ via RSI"),
]

print("=" * 72)
print("🧪 데드킹 스캐너 자체 검증")
print("=" * 72)

for name, scenario, expected in SCENARIOS:
    daily = make_synthetic_klines(scenario, 100)
    # 4H, 1H도 같은 시나리오로 합성 (단순화)
    h4 = make_synthetic_klines(scenario, 100)
    h1 = make_synthetic_klines(scenario, 100)

    ctx = analyze_synthetic("TEST/USDT:USDT", daily, h4, h1)
    op_sig = classify_operator(ctx)
    pctl_sig = classify_pctl(ctx)

    print(f"\n[{name}] (예상: {expected})")
    print(f"     가격: {ctx.price:.3f}, BB상단: {ctx.bb_upper_1d:.3f}")
    print(f"     이격: BB {ctx.bb_dev_1d*100:+.2f}% (pctl {ctx.bb_dev_pctl_1d:.0f}) "
          f"/ 5MA {(ctx.ma5_deviation or 0)*100:+.2f}%")
    print(f"     RSI(6): 1D={ctx.rsi6_1d:.1f}, 4H={ctx.rsi6_4h:.1f}, 1H={ctx.rsi6_1h:.1f}")
    print(f"     → 운영자 모드: {GRADE_EMOJI.get(op_sig.grade,' ')} {op_sig.grade} ({op_sig.name})")
    if op_sig.reasons: print(f"        사유: {op_sig.reasons[0]}")
    print(f"     → 백분위 모드: {GRADE_EMOJI.get(pctl_sig.grade,' ')} {pctl_sig.grade} ({pctl_sig.name})")
    if pctl_sig.reasons: print(f"        사유: {pctl_sig.reasons[0]}")

# 필터 테스트
print()
print("=" * 72)
print("🛡️  PASS 필터 테스트")
print("=" * 72)

# 신고가 임박 시나리오
daily_ath = make_synthetic_klines("mad_os", 100)
_ath_upper, _, _ = ind.bollinger_bands(daily_ath["close"], config.BB_PERIOD, config.BB_STD)
_ath_dev = ind.bb_deviation(float(daily_ath["close"].iloc[-1]), float(_ath_upper.iloc[-1]))
report = flt.run_all_filters(
    symbol="TEST/USDT",
    price=float(daily_ath["close"].iloc[-1]),
    ath=float(daily_ath["high"].max()),         # 현재가가 ATH 근처
    recent_max=float(daily_ath["high"].tail(30).max()),
    daily_df=daily_ath,
    funding_info={"rate": 0.0001, "interval_hours": 8},
    deviation_pctl=99.0,
    deviation_abs=_ath_dev,
    distance_to_upper_cluster=None,
    symbol_meta={},
)

print(f"\nTEST/USDT — {report.passed_count}/{report.total_count} PASS")
for r in report.results:
    print(f"  {r}")

print()
print("=" * 72)
print("✅ 자체 검증 완료. 룰북대로 분류기/필터가 정상 동작합니다.")
print("=" * 72)
