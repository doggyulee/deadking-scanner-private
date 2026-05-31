"""
기술적 지표 계산
- 볼린저밴드 (22, 2)
- RSI (length=6, Wilder's smoothing)
- 5MA, 이격도
- 아래꼬리 비율
"""
from __future__ import annotations

import numpy as np
import pandas as pd


# ============================================================
# 볼린저밴드
# ============================================================
def bollinger_bands(
    close: pd.Series, period: int = 22, std: float = 2.0
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """볼린저밴드 (상단, 중앙, 하단) 반환."""
    middle = close.rolling(window=period, min_periods=period).mean()
    rolling_std = close.rolling(window=period, min_periods=period).std(ddof=0)
    upper = middle + std * rolling_std
    lower = middle - std * rolling_std
    return upper, middle, lower


def bb_deviation(price: float, bb_upper: float) -> float:
    """
    BB 상단 대비 가격의 이격도 (소수).
    양수 = 가격이 BB 상단 위로 튀어나옴 (오버슈팅)
    음수 = 가격이 BB 상단 아래
    """
    if bb_upper is None or bb_upper == 0 or np.isnan(bb_upper):
        return float("nan")
    return (price - bb_upper) / bb_upper


def bb_deviation_series(close: pd.Series, period: int = 22, std: float = 2.0) -> pd.Series:
    """전체 시계열에 대한 BB 이격도 시리즈."""
    upper, _, _ = bollinger_bands(close, period, std)
    return (close - upper) / upper


def deviation_percentile(historical_devs: pd.Series, current_dev: float) -> float:
    """
    현재 이격도가 과거 분포에서 어느 백분위인가.
    종목마다 변동성이 달라서 절대값이 아닌 상대적 위치로 판단.
    (룰북 §3 운영자 본인 인정: "종목마다 다르다")
    """
    valid = historical_devs.dropna()
    if len(valid) == 0 or np.isnan(current_dev):
        return 0.0
    return (valid < current_dev).sum() / len(valid) * 100.0


# ============================================================
# RSI (Wilder's smoothing — TradingView 기본과 동일)
# ============================================================
def rsi(close: pd.Series, length: int = 6) -> pd.Series:
    """
    RSI (Wilder). length=6은 운영자 확정값 (단타용).
    """
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)

    # Wilder smoothing: alpha = 1/length
    avg_gain = gain.ewm(alpha=1 / length, adjust=False, min_periods=length).mean()
    avg_loss = loss.ewm(alpha=1 / length, adjust=False, min_periods=length).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi_val = 100 - (100 / (1 + rs))
    rsi_val = rsi_val.fillna(100)  # loss=0이면 RSI=100
    return rsi_val


# ============================================================
# 이평선
# ============================================================
def sma(close: pd.Series, period: int) -> pd.Series:
    return close.rolling(window=period, min_periods=period).mean()


def ma_deviation(price: float, ma_val: float) -> float:
    """이평선 대비 가격 이격도 (소수)."""
    if ma_val is None or ma_val == 0 or np.isnan(ma_val):
        return float("nan")
    return (price - ma_val) / ma_val


# ============================================================
# 아래꼬리 비율 (계단식 상승 검출)
# ============================================================
def lower_wick_ratio(ohlc: pd.DataFrame) -> pd.Series:
    """
    각 봉의 아래꼬리 / 전체 범위 비율.
    룰북 §7-1: 3D/주봉 아래꼬리 비율 ≥ 30% = 계단식 상승 = 자전거래
    """
    body_low = ohlc[["open", "close"]].min(axis=1)
    total_range = ohlc["high"] - ohlc["low"]
    lower_wick = body_low - ohlc["low"]
    ratio = lower_wick / total_range.replace(0, np.nan)
    return ratio.fillna(0.0)


def avg_lower_wick_ratio(ohlc: pd.DataFrame, lookback: int = 6) -> float:
    """최근 N개 봉의 아래꼬리 비율 평균."""
    ratios = lower_wick_ratio(ohlc).tail(lookback)
    if len(ratios) == 0:
        return 0.0
    return float(ratios.mean())


# ============================================================
# GAP 감지 (전일 종가 vs 당일 시가)
# ============================================================
def detect_price_gap(daily_df: pd.DataFrame, threshold: float = 0.03) -> tuple[bool, float]:
    """
    일봉 시퀀스에서 최신 캔들이 직전 종가 대비 시가 갭이 임계값을 초과하는지.
    운영자 'GAP' 태그와 매칭하기 위한 함수.

    Args:
        daily_df: ohlcv DataFrame (timestamp 인덱스, open/close 컬럼 필수, 최소 2행)
        threshold: 절대값 임계 (기본 0.03 = 3%)

    Returns:
        (is_gap, gap_pct)  — gap_pct 는 부호 있는 값.
        양수 = 갭업(시가가 전일 종가보다 위), 음수 = 갭다운.
        데이터 부족 시 (False, 0.0).
    """
    if daily_df is None or len(daily_df) < 2:
        return False, 0.0
    prev_close = float(daily_df["close"].iloc[-2])
    today_open = float(daily_df["open"].iloc[-1])
    if prev_close <= 0 or np.isnan(prev_close) or np.isnan(today_open):
        return False, 0.0
    gap_pct = (today_open - prev_close) / prev_close
    return abs(gap_pct) > threshold, gap_pct


# ============================================================
# OHLCV → DataFrame 변환 헬퍼
# ============================================================
def ohlcv_to_df(ohlcv: list) -> pd.DataFrame:
    """
    ccxt fetch_ohlcv 결과를 DataFrame으로 변환.
    [[timestamp, open, high, low, close, volume], ...]
    """
    if not ohlcv:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
    df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.set_index("timestamp")
    return df.astype({"open": "float64", "high": "float64", "low": "float64", "close": "float64", "volume": "float64"})
