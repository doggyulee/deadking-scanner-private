"""AGT 케이스 (BB +14% / 5MA +50%) 분류 디버깅.

운영자 5/24 신호: AGT 14:12-13 Mon→OS (모니터링 → OS suffix)
스캐너가 정확히 같은 zone (MONITORING) 으로 분류하는지, 그리고
AND 조건 (5MA 게이트 통과 + BB 게이트 통과) 이 의도대로 작동하는지 검증.
"""
from __future__ import annotations

import sys
from pathlib import Path

# 모듈 내부 import 경로 일치
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import config
from classifier import SignalContext, classify, classify_operator, classify_pctl


def _ctx(symbol: str, *, bb_dev: float, ma_dev: float,
         bb_pctl: float = 88.0,
         rsi_1d: float | None = 90.0,
         rsi_4h: float | None = 70.0,
         rsi_1h: float | None = 65.0) -> SignalContext:
    """BB 상단 대비 dev, 5MA 대비 dev 만 의미있게 채운 가짜 컨텍스트."""
    bb_upper = 1.0
    price = bb_upper * (1 + bb_dev)
    return SignalContext(
        symbol=symbol, price=price,
        bb_upper_1d=bb_upper, bb_middle_1d=0.9, bb_lower_1d=0.8,
        bb_dev_1d=bb_dev, bb_dev_pctl_1d=bb_pctl,
        rsi6_1d=rsi_1d, rsi6_4h=rsi_4h, rsi6_1h=rsi_1h,
        ma5_1d=price / (1 + ma_dev),
        ma5_deviation=ma_dev,
    )


def _dump(label: str, sig) -> None:
    print(f"\n[{label}] {sig.symbol}")
    print(f"  grade={sig.grade}  name={sig.name}  weight={sig.weight}")
    print(f"  mode={sig.mode}  op_grade={sig.op_grade}  pctl_grade={sig.pctl_grade}")
    for r in sig.reasons:
        print(f"   - {r}")


def main() -> int:
    print("=" * 72)
    print(f"운영자 모드 활성: USE_OPERATOR_THRESHOLDS={config.USE_OPERATOR_THRESHOLDS}")
    print(f"운영자 임계값 (BB / 5MA):")
    print(f"  Monitoring  BB ≥ {config.OP_MONITORING_BB*100:.0f}%  AND  5MA ≥ {config.OP_MONITORING_5MA*100:.0f}%")
    print(f"  GoodShort   BB ≥ {config.OP_GOOD_SHORT_BB*100:.0f}%  AND  5MA ≥ {config.OP_GOOD_SHORT_5MA*100:.0f}%")
    print(f"  MAD OS      BB ≥ {config.OP_MAD_OS_BB*100:.0f}%  AND  5MA ≥ {config.OP_MAD_OS_5MA*100:.0f}%")
    print("=" * 72)

    # --- AGT 본 케이스: BB +14%, 5MA +50% ---
    agt = _ctx("AGT/USDT:USDT", bb_dev=0.14, ma_dev=0.50)
    main_sig = classify(agt)
    op_sig = classify_operator(agt)
    pctl_sig = classify_pctl(agt)
    _dump("MAIN  (config 결정)", main_sig)
    _dump("OPERATOR 단독", op_sig)
    _dump("PCTL 단독", pctl_sig)

    # --- 경계 검증: BB 살짝 못 미침 / 살짝 넘김 ---
    print("\n" + "-" * 72)
    print("경계 테스트 (5MA 50% 고정, BB만 변화)")
    for bb in (0.14, 0.149, 0.150, 0.151, 0.20):
        s = classify_operator(_ctx(f"AGT_BB{bb*100:.1f}", bb_dev=bb, ma_dev=0.50))
        print(f"  BB +{bb*100:5.2f}%  →  grade={s.grade:<5}  ({s.name})")

    print("\n" + "-" * 72)
    print("경계 테스트 (BB 14% 고정, 5MA만 변화)")
    for ma in (0.10, 0.13, 0.14, 0.29, 0.30, 0.50):
        s = classify_operator(_ctx(f"AGT_5MA{ma*100:.0f}", bb_dev=0.14, ma_dev=ma))
        print(f"  5MA +{ma*100:5.1f}%  →  grade={s.grade:<5}  ({s.name})")

    # --- 단언 ---
    print("\n" + "=" * 72)
    expect = "C"  # zone=MONITORING + no suffix (BB 14% < 30% MAD, no history, RSI not extreme)
    ok = main_sig.grade == expect
    print(f"단언: AGT 메인 등급 == '{expect}' (Monitoring) : {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
