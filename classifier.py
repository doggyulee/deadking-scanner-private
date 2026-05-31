"""
6등급 시그널 분류 (룰북 §3 — 운영자 정식 공개 신호 체계)

| 등급 | 신호                    | 조건                              | 비중   |
|------|------------------------|----------------------------------|--------|
| S+   | Good Short Zone MAD OS | 굿쇼존 + 미친 오버슈팅            | 0.4%   |
| S    | Good Short Zone OS     | 굿쇼존 + 오버슈팅                 | 0.2%   |
| A    | Good Short Zone        | 이격 충분 (90+퍼센타일)            | 0.1%   |
| B    | Monitoring MAD OS      | 모니터링 + 미친 오버슈팅          | 0.1%   |
| C+   | Monitoring OS          | 모니터링 + 오버슈팅               | 0.05%  |
| C    | Monitoring             | 숏존 진입 (와치만)                | -      |
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Optional

import config


# 등급 → 비중 매핑 (룰북 §4-2)
GRADE_WEIGHTS = {
    "S+": 0.004,   # 0.4%
    "S":  0.002,   # 0.2%
    "A":  0.001,   # 0.1%
    "B":  0.001,   # 0.1% (단타, 경험치 필요)
    "C+": 0.0005,  # 0.05%
    "C":  0.0,     # 진입 X
}

GRADE_NAMES = {
    "S+": "Good Short Zone MAD OS",
    "S":  "Good Short Zone OS",
    "A":  "Good Short Zone",
    "B":  "Monitoring MAD OS",
    "C+": "Monitoring OS",
    "C":  "Monitoring",
}

GRADE_EMOJI = {
    "S+": "😈😈😈🔥",
    "S":  "😈🔥",
    "A":  "🔥",
    "B":  "😈😈😈",
    "C+": "😈",
    "C":  "👀",
}

GRADE_ORDER = ["S+", "S", "A", "B", "C+", "C"]


@dataclass
class SignalContext:
    """시그널 분류에 필요한 모든 지표값."""
    symbol: str
    price: float

    # BB / 이격
    bb_upper_1d: float
    bb_middle_1d: float
    bb_lower_1d: float
    bb_dev_1d: float            # (price - bb_upper) / bb_upper
    bb_dev_pctl_1d: float       # 30일 분포 백분위 (0~100)

    # 15분봉 (사이드 DC)
    bb_dev_15m: Optional[float] = None
    bb_dev_pctl_15m: Optional[float] = None

    # RSI
    rsi6_1d: Optional[float] = None
    rsi6_4h: Optional[float] = None
    rsi6_1h: Optional[float] = None

    # MA
    ma5_1d: Optional[float] = None
    ma5_deviation: Optional[float] = None

    # 메타
    volume_24h_usd: Optional[float] = None
    funding_rate: Optional[float] = None
    funding_interval_h: Optional[int] = None


@dataclass
class Signal:
    """분류된 시그널."""
    symbol: str
    grade: str            # S+ / S / A / B / C+ / C / NONE
    name: str             # 신호명 (한글)
    weight: float         # 권장 진입 비중 (시드 대비)
    reasons: list[str]    # 분류 사유
    context: SignalContext
    mode: str = "percentile"           # "operator" or "percentile" — 어느 모드로 분류됐는지
    op_grade: Optional[str] = None     # 운영자 모드 분류 결과 (메인이 percentile일 때 보조)
    pctl_grade: Optional[str] = None   # 백분위 모드 분류 결과 (메인이 operator일 때 보조)
    tags: list[str] = None             # 메타 태그 (GAP, 골든타임 등) — None=빈 리스트 처리
    gap_pct: Optional[float] = None    # 전일 종가 대비 당일 시가 갭 (소수, 부호 있음)

    def __post_init__(self):
        if self.tags is None:
            self.tags = []

    @property
    def is_entry(self) -> bool:
        """진입 가능 등급인지."""
        return self.grade in ("S+", "S", "A", "B", "C+")

    @property
    def is_strong(self) -> bool:
        """즉시 진입 권장 등급."""
        return self.grade in ("S+", "S")

    @property
    def is_alpha(self) -> bool:
        """백분위 모드와 운영자 모드 결과가 다름 = 스캐너 알파."""
        if self.op_grade is None or self.pctl_grade is None:
            return False
        return self.op_grade != self.pctl_grade

    def to_dict(self) -> dict:
        d = asdict(self)
        d["context"] = asdict(self.context)
        return d


# ============================================================
# 공통 헬퍼: zone+suffix → 등급
# ============================================================
def _zone_suffix_to_grade(zone: str, suffix: str) -> str:
    """('GOOD_SHORT'|'MONITORING'|'NONE', 'MAD_OS'|'OS'|'') → 등급."""
    if zone == "GOOD_SHORT":
        return {"MAD_OS": "S+", "OS": "S"}.get(suffix, "A")
    if zone == "MONITORING":
        return {"MAD_OS": "B", "OS": "C+"}.get(suffix, "C")
    return "NONE"


def _rsi_mad_boost(ctx: SignalContext) -> tuple[bool, list[str]]:
    """4H/1H/1D 중 2개 이상 RSI(6) ≥ 99면 MAD 격상."""
    extremes = []
    for tf, v in [("4H", ctx.rsi6_4h), ("1H", ctx.rsi6_1h), ("1D", ctx.rsi6_1d)]:
        if v is not None and v >= config.RSI_EXTREME:
            extremes.append(f"{tf}={v:.1f}")
    return len(extremes) >= 2, extremes


# ============================================================
# 분류 — 백분위 모드 (스캐너 알파)
# ============================================================
def classify_pctl(ctx: SignalContext) -> Signal:
    """종목별 30일 분포 백분위로 base zone, BB 상단 이격으로 suffix."""
    reasons = []

    # ── 절대값 가드 (거짓 양성 차단) ──────────────
    # 백분위만 높고 절대값 작은/음수 케이스는 익절 거리 부족 → 분류 단계에서 컷
    dev = ctx.bb_dev_1d
    if dev < 0:
        return Signal(
            symbol=ctx.symbol, grade="NONE", name="신호 없음", weight=0.0,
            reasons=[f"BB상단 대비 {dev*100:+.2f}% (음수 = BB 상단 아래, 거짓 양성)"],
            context=ctx, mode="percentile",
        )
    if dev < config.MIN_ABS_BB_DEVIATION:
        return Signal(
            symbol=ctx.symbol, grade="NONE", name="신호 없음", weight=0.0,
            reasons=[f"BB상단 대비 +{dev*100:.2f}% < 최소 {config.MIN_ABS_BB_DEVIATION*100:.1f}% (익절 거리 부족)"],
            context=ctx, mode="percentile",
        )

    pctl = ctx.bb_dev_pctl_1d
    if pctl >= config.GOOD_SHORT_PCTL:
        zone = "GOOD_SHORT"
        reasons.append(f"일봉 BB 이격 {pctl:.0f}퍼센타일 (≥{config.GOOD_SHORT_PCTL}) = 굿쇼존")
    elif pctl >= config.MONITORING_PCTL:
        zone = "MONITORING"
        reasons.append(f"일봉 BB 이격 {pctl:.0f}퍼센타일 (≥{config.MONITORING_PCTL}) = 모니터링")
    else:
        return Signal(
            symbol=ctx.symbol, grade="NONE", name="신호 없음", weight=0.0,
            reasons=[f"일봉 BB 이격 {pctl:.0f}퍼센타일 (이격 부족)"],
            context=ctx, mode="percentile",
        )

    suffix = ""
    if dev >= config.MAD_OS_DEVIATION:
        suffix = "MAD_OS"
        reasons.append(f"BB상단 대비 +{dev*100:.2f}% (≥{config.MAD_OS_DEVIATION*100:.1f}%) = MAD OS")
    elif dev >= config.OS_DEVIATION:
        suffix = "OS"
        reasons.append(f"BB상단 대비 +{dev*100:.2f}% (≥{config.OS_DEVIATION*100:.1f}%) = OS")
    else:
        reasons.append(f"BB상단 대비 {dev*100:+.2f}% (오버슈팅 아님)")

    boost, extremes = _rsi_mad_boost(ctx)
    if boost and suffix != "MAD_OS":
        suffix = "MAD_OS"
        reasons.append(f"RSI 99 극단치 다수 ({', '.join(extremes)}) → MAD 격상")
    elif extremes:
        reasons.append(f"RSI 극단치: {', '.join(extremes)}")

    grade = _zone_suffix_to_grade(zone, suffix)
    return Signal(
        symbol=ctx.symbol, grade=grade, name=GRADE_NAMES[grade],
        weight=GRADE_WEIGHTS[grade], reasons=reasons, context=ctx,
        mode="percentile",
    )


# ============================================================
# 분류 — 운영자 절대 임계값 모드
# ============================================================
def classify_operator(ctx: SignalContext, history_suffix: str = "") -> Signal:
    """
    운영자 5/22-23 로그 기반 절대 임계값.
    5MA 이격을 1순위 게이트로, BB 이격을 2순위로 (AND 조건).
    OS/MAD OS suffix는 두 가지 입력:
      (1) 절대 임계값 즉시 격상: BB ≥ 25% AND 5MA ≥ 45% → MAD OS
      (2) history_suffix: 시간 추적으로 측정한 추가 상승률 기반
    """
    reasons = []
    bb_dev = ctx.bb_dev_1d
    ma_dev = ctx.ma5_deviation

    if ma_dev is None:
        return Signal(
            symbol=ctx.symbol, grade="NONE", name="신호 없음", weight=0.0,
            reasons=["5MA 데이터 없음"],
            context=ctx, mode="operator",
        )

    # ── 1순위: 5MA 게이트 ────────────────────────────
    if ma_dev >= config.OP_GOOD_SHORT_5MA and bb_dev >= config.OP_GOOD_SHORT_BB:
        zone = "GOOD_SHORT"
        reasons.append(
            f"5MA +{ma_dev*100:.1f}% (≥{config.OP_GOOD_SHORT_5MA*100:.0f}%) "
            f"AND BB +{bb_dev*100:.1f}% (≥{config.OP_GOOD_SHORT_BB*100:.0f}%) = 굿쇼존"
        )
    elif ma_dev >= config.OP_MONITORING_5MA and bb_dev >= config.OP_MONITORING_BB:
        zone = "MONITORING"
        reasons.append(
            f"5MA +{ma_dev*100:.1f}% (≥{config.OP_MONITORING_5MA*100:.0f}%) "
            f"AND BB +{bb_dev*100:.1f}% (≥{config.OP_MONITORING_BB*100:.0f}%) = 모니터링"
        )
    else:
        return Signal(
            symbol=ctx.symbol, grade="NONE", name="신호 없음", weight=0.0,
            reasons=[
                f"5MA +{ma_dev*100:.1f}% / BB +{bb_dev*100:.1f}% — 운영자 임계값 미달"
            ],
            context=ctx, mode="operator",
        )

    # ── 2순위: 절대 임계값 즉시 MAD ─────────────────
    suffix = ""
    if bb_dev >= config.OP_MAD_OS_BB and ma_dev >= config.OP_MAD_OS_5MA:
        suffix = "MAD_OS"
        reasons.append(
            f"BB +{bb_dev*100:.1f}% AND 5MA +{ma_dev*100:.1f}% "
            f"≥ 운영자 MAD 임계 ({config.OP_MAD_OS_BB*100:.0f}/{config.OP_MAD_OS_5MA*100:.0f}) → MAD OS"
        )

    # ── 3순위: history 시간 추적 suffix ─────────────
    if history_suffix and not suffix:
        suffix = history_suffix
        reasons.append(f"시간 추적: 첫 신호 이후 추가 상승 → {suffix}")
    elif history_suffix == "MAD_OS" and suffix == "OS":
        suffix = "MAD_OS"
        reasons.append("시간 추적: 추가 +3% 상승 → MAD OS 격상")

    # ── 4순위: RSI 99 다중 TF → MAD ──────────────────
    boost, extremes = _rsi_mad_boost(ctx)
    if boost and suffix != "MAD_OS":
        suffix = "MAD_OS"
        reasons.append(f"RSI 99 극단치 다수 ({', '.join(extremes)}) → MAD 격상")
    elif extremes:
        reasons.append(f"RSI 극단치: {', '.join(extremes)}")

    grade = _zone_suffix_to_grade(zone, suffix)
    return Signal(
        symbol=ctx.symbol, grade=grade, name=GRADE_NAMES[grade],
        weight=GRADE_WEIGHTS[grade], reasons=reasons, context=ctx,
        mode="operator",
    )


# ============================================================
# 통합 분류 (두 모드 모두 돌려 main + 보조)
# ============================================================
def classify(ctx: SignalContext, history_suffix: str = "") -> Signal:
    """
    두 모드 모두 분류해 메인 등급을 config.USE_OPERATOR_THRESHOLDS로 결정.
    반대편 결과는 보조 필드(op_grade / pctl_grade)로 첨부.
    """
    op_sig = classify_operator(ctx, history_suffix=history_suffix)
    pctl_sig = classify_pctl(ctx)

    if config.USE_OPERATOR_THRESHOLDS:
        main = op_sig
        main.op_grade = op_sig.grade
        main.pctl_grade = pctl_sig.grade
    else:
        main = pctl_sig
        main.op_grade = op_sig.grade
        main.pctl_grade = pctl_sig.grade
    return main


# ============================================================
# 등급 비교 헬퍼
# ============================================================
def grade_at_least(grade: str, minimum: str) -> bool:
    """grade가 minimum 이상인가."""
    if grade == "NONE":
        return False
    return GRADE_ORDER.index(grade) <= GRADE_ORDER.index(minimum)
