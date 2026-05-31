"""
거래소 API 래퍼 (ccxt 기반)
- USDT 무기한 선물 심볼 목록
- OHLCV (다중 TF)
- 펀딩비
- ATH
"""
from __future__ import annotations

import logging
import time
from typing import Optional

import ccxt
import pandas as pd

import config

log = logging.getLogger(__name__)


# ============================================================
# 거래소 인스턴스
# ============================================================
def make_exchange(exchange_id: str = None) -> ccxt.Exchange:
    """
    바이낸스 / 비트겟 등 거래소 객체 생성.
    공개 데이터만 사용하므로 API 키 불필요.
    """
    exchange_id = exchange_id or config.EXCHANGE_ID
    klass = getattr(ccxt, exchange_id)
    ex = klass({
        "enableRateLimit": True,
        "timeout": config.REQUEST_TIMEOUT_SEC * 1000,
        "options": {
            "defaultType": "swap" if config.MARKET_TYPE == "swap" else "future",
        },
    })
    return ex


# ============================================================
# 심볼 목록
# ============================================================
def fetch_usdt_perp_symbols(
    exchange: ccxt.Exchange,
    top_n: int = None,
    min_volume_usd: float = None,
) -> list[dict]:
    """
    USDT 무기한 선물 심볼 목록 반환.
    24시간 거래대금 기준 상위 N개로 필터링.

    Returns: [{"symbol": "BTC/USDT:USDT", "volume_usd": ...}, ...]
    """
    top_n = top_n or config.TOP_N_BY_VOLUME
    min_volume_usd = min_volume_usd if min_volume_usd is not None else config.MIN_24H_VOLUME_USD

    log.info("심볼 목록 로딩 중...")
    markets = exchange.load_markets()

    # USDT 무기한만 필터
    perp_symbols = [
        s for s, m in markets.items()
        if m.get("swap") and m.get("quote") == config.QUOTE and m.get("active", True)
    ]
    log.info(f"USDT 무기한 심볼: {len(perp_symbols)}개")

    # 24h 티커로 거래대금 정렬
    tickers = exchange.fetch_tickers(perp_symbols)
    rows = []
    for sym in perp_symbols:
        t = tickers.get(sym, {})
        # quoteVolume = USDT 환산 거래대금
        vol = t.get("quoteVolume") or 0
        if vol < min_volume_usd:
            continue
        # 제외 키워드 / 제외 심볼
        base = markets[sym].get("base", "")
        if base in (config.EXCLUDE_SYMBOLS or []):
            continue
        if any(kw in base.upper() for kw in (config.EXCLUDE_KEYWORDS or [])):
            continue
        rows.append({
            "symbol": sym,
            "base": base,
            "volume_usd": float(vol),
            "last": float(t.get("last") or 0),
        })

    rows.sort(key=lambda r: r["volume_usd"], reverse=True)
    rows = rows[:top_n]
    log.info(f"스캔 대상: {len(rows)}개 (거래대금 ≥ ${min_volume_usd:,.0f})")
    return rows


# ============================================================
# OHLCV
# ============================================================
def fetch_ohlcv(
    exchange: ccxt.Exchange,
    symbol: str,
    timeframe: str,
    limit: int,
    retry: int = None,
) -> Optional[list]:
    """
    OHLCV 캔들 페치 (재시도 포함).
    """
    retry = retry or config.RETRY_MAX
    for attempt in range(retry):
        try:
            return exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        except ccxt.NetworkError as e:
            log.warning(f"{symbol} {timeframe} 네트워크 오류 (시도 {attempt+1}/{retry}): {e}")
            time.sleep(2 ** attempt)
        except ccxt.ExchangeError as e:
            log.warning(f"{symbol} {timeframe} 거래소 오류: {e}")
            return None
        except Exception as e:
            log.error(f"{symbol} {timeframe} 알 수 없는 오류: {e}")
            return None
    return None


def fetch_multi_tf(
    exchange: ccxt.Exchange,
    symbol: str,
) -> dict[str, list]:
    """
    한 심볼에 대해 일봉/4시간/1시간 OHLCV 모두 페치.
    """
    return {
        config.TF_DAILY: fetch_ohlcv(exchange, symbol, config.TF_DAILY, config.DAILY_LOOKBACK_DAYS),
        config.TF_4H: fetch_ohlcv(exchange, symbol, config.TF_4H, config.H4_LOOKBACK_BARS),
        config.TF_1H: fetch_ohlcv(exchange, symbol, config.TF_1H, config.H1_LOOKBACK_BARS),
    }


# ============================================================
# 펀딩비
# ============================================================
def fetch_funding_rate(exchange: ccxt.Exchange, symbol: str) -> Optional[dict]:
    """
    현재 펀딩비 정보.
    Returns: {"rate": 0.0001, "interval_hours": 8, ...} or None
    """
    try:
        fr = exchange.fetch_funding_rate(symbol)
        # 펀딩 주기 추정 (바이낸스 = 8h, 비트겟 = 8h, 일부 코인 1h/4h)
        # ccxt 거래소별로 interval 표기가 제각각:
        #   - 숫자(시간) — 바이낸스
        #   - "8h" / "4h" / "1h" — 비트겟
        #   - None — 폴백 8h
        raw_interval = fr.get("interval")
        interval_hours = _parse_funding_interval(raw_interval)
        return {
            "rate": float(fr.get("fundingRate") or 0),
            "next_funding_time": fr.get("fundingDatetime"),
            "interval_hours": interval_hours,
            "symbol": symbol,
        }
    except Exception as e:
        log.warning(f"{symbol} 펀딩비 페치 실패: {e}")
        return None


def _parse_funding_interval(raw) -> float:
    """ccxt가 거래소별로 다르게 주는 펀딩 주기를 시간(float)으로 정규화."""
    if raw is None:
        return 8.0
    if isinstance(raw, (int, float)):
        return float(raw) or 8.0
    if isinstance(raw, str):
        s = raw.strip().lower()
        if s.endswith("h"):
            try:
                return float(s[:-1]) or 8.0
            except ValueError:
                return 8.0
        if s.endswith("m"):  # 분 단위 → 시간 환산
            try:
                return (float(s[:-1]) / 60.0) or 8.0
            except ValueError:
                return 8.0
        try:
            return float(s) or 8.0
        except ValueError:
            return 8.0
    return 8.0


# ============================================================
# ATH (Bar 데이터로부터 추정)
# ============================================================
def estimate_ath_from_klines(daily_df: pd.DataFrame) -> float:
    """
    페치한 일봉 데이터 범위 내 최고가 = 근사 ATH.
    정확한 상장 이래 ATH는 별도 페치가 필요하지만,
    100일 기준이면 대부분 케이스에서 충분.
    """
    if daily_df is None or len(daily_df) == 0:
        return float("nan")
    return float(daily_df["high"].max())


def recent_high(daily_df: pd.DataFrame, days: int = 30) -> float:
    """최근 N일 고가."""
    if daily_df is None or len(daily_df) < days:
        return float("nan")
    return float(daily_df["high"].tail(days).max())
