"""오늘(2026-05-24) 운영자 신호 vs 스캐너 분류 매칭률 계산.

운영자 채널 7개 신호:
  BAN     0:36   Mon
  GRASS   1:23-1:45  Mon→OS
  NEAR    7:00   Mon
  IN      8:08-8:45  Mon→OS→OS
  BEAT    9:05   GoodShort SCAM
  BSB     9:16   Mon
  AGT     14:12-13  Mon→OS
  GENIUS  15:35  Mon (SCAM?)        # 비교 대상이지만 동시에 차단 가드 케이스

각 종목별 운영자 zone (GOOD_SHORT vs MONITORING) 를 메인 메트릭으로 비교.
suffix (OS / MAD OS) 는 시간 추적 결과라 스냅샷 1회 분류로는 못 맞춤.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# 운영자 zone 정답 라벨 (suffix는 history 기반이라 zone만 비교)
OPERATOR_ZONE = {
    "BAN":    "MONITORING",
    "GRASS":  "MONITORING",   # Mon→OS, zone = Mon
    "NEAR":   "MONITORING",
    "IN":     "MONITORING",   # Mon→OS→OS
    "BEAT":   "GOOD_SHORT",   # GoodShort
    "BSB":    "MONITORING",
    "AGT":    "MONITORING",   # Mon→OS, zone = Mon
    # GENIUS는 별도 (Mon이지만 SCAM?, 우리 가드 차단 비교는 별도 섹션)
}

# 스캐너 등급 → zone 매핑
GRADE_TO_ZONE = {
    "S+": "GOOD_SHORT", "S": "GOOD_SHORT", "A": "GOOD_SHORT",
    "B": "MONITORING", "C+": "MONITORING", "C": "MONITORING",
    "NONE": "NONE",
}


def base_symbol(sym: str) -> str:
    """'BAN/USDT:USDT' → 'BAN'."""
    return sym.split("/", 1)[0]


def load_signals(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        payload = json.load(f)
    out = {}
    for s in payload.get("signals", []):
        out[base_symbol(s["symbol"])] = s
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("scan_json", nargs="?", default=None,
                    help="비교할 scan_*.json 파일 (생략시 최신)")
    args = ap.parse_args()

    out_dir = HERE / "output"
    if args.scan_json:
        path = Path(args.scan_json)
    else:
        candidates = sorted(out_dir.glob("scan_*.json"))
        if not candidates:
            print("scan_*.json 없음. 먼저 스캔 실행.")
            return 2
        path = candidates[-1]
    print(f"분석 대상: {path.name}")

    signals = load_signals(path)

    print()
    print("=" * 78)
    print("운영자 vs 스캐너 zone 매칭")
    print("=" * 78)
    print(f"  {'심볼':<8} {'운영자':<14} {'스캐너 등급':<14} {'스캐너 zone':<14} {'매칭':<6}")
    print("  " + "-" * 70)

    matches = 0
    misses = []
    for sym, op_zone in OPERATOR_ZONE.items():
        sig = signals.get(sym)
        if sig is None:
            print(f"  {sym:<8} {op_zone:<14} {'(스캔에 없음)':<14} {'NONE':<14} {'❌':<6}")
            misses.append(sym)
            continue
        grade = sig.get("grade", "NONE")
        sc_zone = GRADE_TO_ZONE.get(grade, "?")
        ok = sc_zone == op_zone
        if ok:
            matches += 1
        else:
            misses.append(sym)
        print(f"  {sym:<8} {op_zone:<14} {grade:<14} {sc_zone:<14} {'✅' if ok else '❌':<6}")

    total = len(OPERATOR_ZONE)
    rate = matches / total * 100
    print()
    print(f"매칭률: {matches}/{total} = {rate:.1f}%")
    if misses:
        print(f"미매칭: {', '.join(misses)}")

    # GENIUS 별도 확인 (가드 차단 vs 운영자 SCAM? 일치)
    print()
    print("=" * 78)
    print("GENIUS 별도 — 절대값 가드 차단 vs 운영자 SCAM? 태그")
    print("=" * 78)
    g = signals.get("GENIUS")
    if g is None:
        print("  스캔 결과에 GENIUS 없음 (가드/필터 모두 통과 못 함 = 차단됨)")
        print("  운영자: 'GENIUS 15:35 Mon SCAM?' (Monitoring + 사기 의심)")
        print("  → 우리 가드 차단 결과가 운영자 의심과 일치 (가드 적중)")
    else:
        ctx = g.get("context", {})
        bb = (ctx.get("bb_dev_1d") or 0) * 100
        ma = (ctx.get("ma5_deviation") or 0) * 100
        print(f"  스캔에 포함 — grade={g.get('grade')} BB {bb:+.2f}%  5MA {ma:+.2f}%")
        print(f"  운영자: Mon (SCAM?)")
        print(f"  스캐너 zone: {GRADE_TO_ZONE.get(g.get('grade'), '?')}")

    return 0 if matches == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
