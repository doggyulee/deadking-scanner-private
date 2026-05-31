"""
다이버전스 자동 감지 결과 추적기.

운영자 검색기엔 없는 스캐너 알파:
  1D RSI ≥ 85 인데 4H/1H RSI가 식어있으면 단기 모멘텀 사망 = 회귀 예측 신호.
  실제로 그런가? — 4h / 12h / 24h 후 BB 이격 변화율을 자동 기록해 통계화.

상태 파일: output/divergence_tracker.json
{
  "BSB/USDT:USDT__2026-05-23T20:41:12+00:00": {
    "detected_at":   "2026-05-23T20:41:12+00:00",
    "symbol":        "BSB/USDT:USDT",
    "rsi_1d":        92.2,
    "rsi_4h":        64.7,
    "rsi_1h":        53.7,
    "spread":        27.5,                  # 1D - max(4H, 1H)
    "entry_price":   1.20304,
    "entry_bb_dev":  0.1399,
    "observations": [
      {"t_hours": 4.0,  "price": ..., "bb_dev": ..., "dev_change_pct": ...},
      {"t_hours": 12.0, ...},
      {"t_hours": 24.0, ...}
    ],
    "resolved": false                       # 24h 관측 다 채워지면 true
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
from typing import Optional

import config

log = logging.getLogger(__name__)

TRACKER_FILE = "./output/divergence_tracker.json"

# 관측 시점 (시간 단위)
OBSERVATION_POINTS = [4.0, 12.0, 24.0]
# 관측 허용 오차 (분)
OBSERVATION_TOLERANCE_MIN = 30


# ============================================================
# 다이버전스 감지 (분류 단계와 동일 로직, 별도 노출)
# ============================================================
def detect(rsi_1d: Optional[float],
           rsi_4h: Optional[float],
           rsi_1h: Optional[float]) -> Optional[float]:
    """
    다이버전스 감지. 격차(1D vs 4H/1H 중 큰 값) 반환. 미감지면 None.
    조건: 1D RSI ≥ 85 AND (4H < 70 OR 1H < 65)
       또는 1D ≥ 90 AND 4H < 75 AND 1H < 75
    """
    if rsi_1d is None or rsi_4h is None or rsi_1h is None:
        return None
    if rsi_1d >= 85 and (rsi_4h < 70 or rsi_1h < 65):
        return rsi_1d - max(rsi_4h, rsi_1h)
    if rsi_1d >= 90 and rsi_4h < 75 and rsi_1h < 75:
        return rsi_1d - max(rsi_4h, rsi_1h)
    return None


# ============================================================
# 영속화
# ============================================================
def _now() -> datetime:
    return datetime.now(timezone.utc)


def load() -> dict:
    if not os.path.exists(TRACKER_FILE):
        return {}
    try:
        with open(TRACKER_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        log.warning(f"divergence tracker 로드 실패 ({e}). 새로 시작.")
        return {}


def save(tracker: dict) -> None:
    os.makedirs(os.path.dirname(TRACKER_FILE), exist_ok=True)
    with open(TRACKER_FILE, "w", encoding="utf-8") as f:
        json.dump(tracker, f, ensure_ascii=False, indent=2, default=str)


# ============================================================
# 신규 다이버전스 등록 / 후속 관측 갱신
# ============================================================
@dataclass
class DivergenceUpdate:
    new: bool                          # 이번 스캔에서 새로 등록됐는가
    record_key: str
    pending_observations: list[float]  # 아직 안 채워진 관측 시점들


def _make_key(symbol: str, detected_at: datetime) -> str:
    return f"{symbol}__{detected_at.isoformat()}"


def register_or_update(
    tracker: dict,
    symbol: str,
    rsi_1d: float, rsi_4h: float, rsi_1h: float,
    spread: float,
    price: float, bb_dev: float,
) -> DivergenceUpdate:
    """
    같은 종목에 대해 최근 24시간 내 미해결 다이버전스가 있으면 그것을 업데이트.
    없으면 새 레코드 등록.
    """
    now = _now()
    cutoff = now - timedelta(hours=26)

    # 동일 종목의 미해결 최신 레코드 찾기
    existing_key = None
    for k, rec in tracker.items():
        if rec.get("symbol") != symbol:
            continue
        if rec.get("resolved"):
            continue
        try:
            t = datetime.fromisoformat(rec["detected_at"])
        except (KeyError, ValueError):
            continue
        if t >= cutoff:
            existing_key = k
            break

    if existing_key:
        rec = tracker[existing_key]
        _attach_observation(rec, now, price, bb_dev)
        pending = _pending_points(rec)
        if not pending:
            rec["resolved"] = True
        return DivergenceUpdate(new=False, record_key=existing_key,
                                pending_observations=pending)

    # 새 레코드
    key = _make_key(symbol, now)
    tracker[key] = {
        "detected_at": now.isoformat(),
        "symbol": symbol,
        "rsi_1d": rsi_1d,
        "rsi_4h": rsi_4h,
        "rsi_1h": rsi_1h,
        "spread": spread,
        "entry_price": price,
        "entry_bb_dev": bb_dev,
        "observations": [],
        "resolved": False,
    }
    return DivergenceUpdate(new=True, record_key=key,
                            pending_observations=list(OBSERVATION_POINTS))


def _attach_observation(rec: dict, now: datetime,
                        price: float, bb_dev: float) -> None:
    """관측 시점 중 허용 오차 내에 있는 가장 가까운 t를 골라 기록."""
    detected = datetime.fromisoformat(rec["detected_at"])
    age_h = (now - detected).total_seconds() / 3600.0
    already = {o["t_hours"] for o in rec.get("observations", [])}
    tol_h = OBSERVATION_TOLERANCE_MIN / 60.0

    # 가장 가까운 미관측 시점
    for t in OBSERVATION_POINTS:
        if t in already:
            continue
        if abs(age_h - t) <= tol_h:
            entry_dev = float(rec.get("entry_bb_dev", bb_dev) or 0)
            dev_change = (bb_dev - entry_dev) / abs(entry_dev) if entry_dev else 0.0
            rec.setdefault("observations", []).append({
                "t_hours": t,
                "actual_age_hours": round(age_h, 3),
                "price": price,
                "bb_dev": bb_dev,
                "dev_change_pct": dev_change,
            })
            break


def _pending_points(rec: dict) -> list[float]:
    done = {o["t_hours"] for o in rec.get("observations", [])}
    return [t for t in OBSERVATION_POINTS if t not in done]


# ============================================================
# 통계 리포트
# ============================================================
def summarize(tracker: dict) -> dict:
    """
    누적 다이버전스 케이스의 회귀 예측 정확도.
    "회귀" = 이격이 감소(dev_change_pct < 0)했는가.
    """
    resolved = [r for r in tracker.values() if r.get("resolved")]
    pending = [r for r in tracker.values() if not r.get("resolved")]

    summary = {
        "total_detected": len(tracker),
        "resolved": len(resolved),
        "pending": len(pending),
        "by_horizon": {},
    }
    for t in OBSERVATION_POINTS:
        obs = []
        for r in tracker.values():
            for o in r.get("observations", []):
                if o.get("t_hours") == t:
                    obs.append(o.get("dev_change_pct", 0))
        if obs:
            mean_change = sum(obs) / len(obs)
            n_regress = sum(1 for v in obs if v < 0)
            summary["by_horizon"][f"{t}h"] = {
                "n_observations": len(obs),
                "mean_dev_change_pct": round(mean_change, 4),
                "regression_hit_rate": round(n_regress / len(obs), 3),
            }
    return summary


def print_summary(tracker: dict) -> None:
    s = summarize(tracker)
    print()
    print("=" * 72)
    print("🧬 다이버전스 추적 통계 (스캐너 알파 검증)")
    print("=" * 72)
    print(f"  총 감지: {s['total_detected']}건  "
          f"(완료 {s['resolved']} / 진행중 {s['pending']})")
    if not s["by_horizon"]:
        print("  관측 데이터 부족 — 시간 경과 후 재확인")
        return
    print(f"  {'호라이즌':<10} {'관측수':>6} {'평균 이격변화':>14} {'회귀적중률':>12}")
    for h, v in s["by_horizon"].items():
        print(f"  {h:<10} {v['n_observations']:>6} "
              f"{v['mean_dev_change_pct']*100:>13.2f}% "
              f"{v['regression_hit_rate']*100:>11.1f}%")
