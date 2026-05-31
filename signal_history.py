"""
신호 시간/가격 추적 — 운영자 OS / MAD OS 격상 근사 (v2).

v2 변경:
  v1은 "첫 신호 발생가" 기준이라 횡보 코인도 한 번 오르면 OS로 격상되는 문제.
  v2는 "종목별 누적 최고가" 기준 — 새 최고가 돌파 시에만 OS / MAD OS 격상.
  운영자 방식과 일치 (이미 만든 고점 못 넘는 펌프는 의미 없음).

상태 파일: output/signal_history.json
{
  "BEAT/USDT:USDT": {
    "first_seen":   "2026-05-23T20:41:12+00:00",
    "first_price":  1.4308,
    "cum_high":     1.4500,             # 역대 모든 관측 중 최고가
    "cum_high_at":  "2026-05-23T21:00:00+00:00",
    "last_seen":    "2026-05-23T21:15:00+00:00",
    "last_price":   1.4179,
    "observation_count": 5
  },
  ...
}
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import config

log = logging.getLogger(__name__)


@dataclass
class HistoryDelta:
    """현재 가격 vs 누적 최고가."""
    first_seen: datetime
    first_price: float
    cum_high: float                # 누적 최고가
    cum_high_at: datetime          # 최고가 찍은 시각
    last_price: float              # 직전 관측가
    age_hours: float               # 첫 관측 이후 경과
    pct_vs_cum_high: float         # (now - cum_high) / cum_high  -- 보통 ≤ 0
    is_new_high: bool              # 이번 관측에서 cum_high 갱신했는가
    breakout_pct: float            # 갱신했다면 직전 최고가 대비 돌파 폭

    @property
    def suffix(self) -> str:
        """
        운영자 OS 로직 근사:
          - 신규 고점 + 직전 cum_high 대비 +3% → MAD OS
          - 신규 고점 (소폭) → OS
          - 고점 못 넘으면 suffix 없음 (Good Short Zone)
        """
        if not self.is_new_high:
            return ""
        if self.breakout_pct >= config.HISTORY_MAD_OS_PCT:
            return "MAD_OS"
        if self.breakout_pct >= 0:
            return "OS"
        return ""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def load() -> dict:
    path = config.HISTORY_FILE
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        # v1 포맷 자동 마이그레이션 (max_price 만 있으면 cum_high로 변환)
        for sym, rec in data.items():
            if "cum_high" not in rec and "max_price" in rec:
                rec["cum_high"] = rec.pop("max_price")
                rec.setdefault("cum_high_at", rec.get("last_seen") or rec["first_seen"])
                rec.setdefault("last_price", rec["cum_high"])
                rec.setdefault("observation_count", 1)
        return data
    except (json.JSONDecodeError, OSError) as e:
        log.warning(f"history 파일 로드 실패 ({e}). 새로 시작.")
        return {}


def save(history: dict) -> None:
    path = config.HISTORY_FILE
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2, default=str)


def prune_expired(history: dict) -> dict:
    now = _now()
    cutoff = now - timedelta(hours=config.HISTORY_EXPIRE_HOURS)
    out = {}
    for sym, rec in history.items():
        try:
            last = datetime.fromisoformat(rec["last_seen"])
        except (KeyError, ValueError):
            continue
        if last >= cutoff:
            out[sym] = rec
    return out


def update(history: dict, symbol: str, price: float) -> HistoryDelta:
    """
    현재 가격으로 history 갱신. 누적 최고가 돌파 여부 판정.
    Returns: HistoryDelta
    """
    now = _now()
    now_iso = now.isoformat()
    rec = history.get(symbol)

    if rec is None:
        rec = {
            "first_seen": now_iso,
            "first_price": price,
            "cum_high": price,
            "cum_high_at": now_iso,
            "last_seen": now_iso,
            "last_price": price,
            "observation_count": 1,
        }
        history[symbol] = rec
        is_new_high = False  # 첫 관측은 돌파 아님 (기준이 본인)
        breakout = 0.0
    else:
        prev_high = float(rec.get("cum_high", price))
        is_new_high = price > prev_high
        breakout = (price - prev_high) / prev_high if prev_high > 0 else 0.0

        if is_new_high:
            rec["cum_high"] = price
            rec["cum_high_at"] = now_iso
        rec["last_seen"] = now_iso
        rec["last_price"] = price
        rec["observation_count"] = int(rec.get("observation_count", 0)) + 1

    first_seen = datetime.fromisoformat(rec["first_seen"])
    cum_high = float(rec["cum_high"])
    cum_high_at = datetime.fromisoformat(rec["cum_high_at"])
    age_h = (now - first_seen).total_seconds() / 3600.0
    pct_vs_high = (price - cum_high) / cum_high if cum_high > 0 else 0.0

    return HistoryDelta(
        first_seen=first_seen,
        first_price=float(rec["first_price"]),
        cum_high=cum_high,
        cum_high_at=cum_high_at,
        last_price=price,
        age_hours=age_h,
        pct_vs_cum_high=pct_vs_high,
        is_new_high=is_new_high,
        breakout_pct=breakout,
    )
