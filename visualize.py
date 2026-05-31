"""
스캔 결과 JSON 시각화 (터미널 텍스트 + ANSI 색상).

사용법:
  python visualize.py                          # output/ 최신 스캔
  python visualize.py output/scan_xxx.json     # 특정 파일
  python visualize.py --min-grade A            # 등급 필터
  python visualize.py --no-color               # 색상 끔
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys

# ── ANSI 색상 ────────────────────────────────────────────
class C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    MAGENTA = "\033[95m"
    CYAN = "\033[96m"
    WHITE = "\033[97m"
    BG_RED = "\033[101m"
    BG_GREEN = "\033[102m"
    BG_YELLOW = "\033[103m"


def no_color():
    for attr in dir(C):
        if not attr.startswith("_") and isinstance(getattr(C, attr), str):
            setattr(C, attr, "")


# ── 등급 컬러맵 ──────────────────────────────────────────
GRADE_COLOR = {
    "S+": C.BG_RED + C.WHITE + C.BOLD,
    "S":  C.RED + C.BOLD,
    "A":  C.YELLOW + C.BOLD,
    "B":  C.MAGENTA,
    "C+": C.CYAN,
    "C":  C.DIM,
}

GRADE_RANK = {"S+": 0, "S": 1, "A": 2, "B": 3, "C+": 4, "C": 5, "NONE": 99}


# ── 게이지 ───────────────────────────────────────────────
def gauge(value: float, lo: float, hi: float, width: int = 20,
          marker_at: float | None = None) -> str:
    """수평 게이지. value를 [lo, hi] 구간 위에 막대로 표시."""
    if hi <= lo:
        return "?" * width
    pct = max(0.0, min(1.0, (value - lo) / (hi - lo)))
    pos = int(round(pct * (width - 1)))
    bar = ["─"] * width
    bar[pos] = "█"
    if marker_at is not None:
        m = max(0.0, min(1.0, (marker_at - lo) / (hi - lo)))
        mp = int(round(m * (width - 1)))
        if mp != pos:
            bar[mp] = "┊"
    return "".join(bar)


def rsi_color(v: float | None) -> str:
    if v is None:
        return C.DIM + " --- " + C.RESET
    if v >= 95:
        col = C.BG_RED + C.WHITE
    elif v >= 80:
        col = C.RED + C.BOLD
    elif v >= 70:
        col = C.YELLOW
    elif v >= 50:
        col = C.GREEN
    else:
        col = C.CYAN
    return f"{col}{v:5.1f}{C.RESET}"


def dev_color(pct: float) -> str:
    if pct >= 10:
        return C.BG_RED + C.WHITE + C.BOLD
    if pct >= 5:
        return C.RED + C.BOLD
    if pct >= 1.5:
        return C.YELLOW + C.BOLD
    if pct >= 0:
        return C.YELLOW
    return C.DIM


def funding_color(rate_pct: float) -> str:
    a = abs(rate_pct)
    if rate_pct < -0.05:
        return C.BG_RED + C.WHITE
    if rate_pct < 0:
        return C.RED
    if a > 0.05:
        return C.GREEN + C.BOLD
    return C.GREEN


# ── BB 위치 시각화 ───────────────────────────────────────
def bb_position(price: float, upper: float, middle: float, lower: float) -> str:
    """
    BB 채널 안에서 가격 위치 + 위로 튀어나간 만큼.
    [lower─middle─upper]+   처럼 표시.
    """
    width = 22
    if upper <= lower:
        return "?" * width
    band_lo = lower - (upper - lower) * 0.2
    band_hi = upper + (upper - lower) * 0.5
    if price > band_hi:
        band_hi = price * 1.02
    bar = gauge(price, band_lo, band_hi, width)
    # 채널 경계 표시
    out = list(bar)
    for boundary, sym in [(lower, "["), (middle, "│"), (upper, "]")]:
        m = (boundary - band_lo) / (band_hi - band_lo)
        idx = int(round(m * (width - 1)))
        if 0 <= idx < width and out[idx] not in ("█",):
            out[idx] = sym
    # 색상: 가격이 upper 위면 빨강
    bar_str = "".join(out)
    if price > upper:
        return C.RED + bar_str + C.RESET
    if price > middle:
        return C.YELLOW + bar_str + C.RESET
    return C.GREEN + bar_str + C.RESET


# ── 다이버전스 감지 ──────────────────────────────────────
def divergence_warning(rsi_1d, rsi_4h, rsi_1h) -> str | None:
    """일봉 RSI는 극단인데 4H/1H가 식고 있으면 경고."""
    if rsi_1d is None or rsi_4h is None or rsi_1h is None:
        return None
    if rsi_1d >= 85 and (rsi_4h < 70 or rsi_1h < 65):
        spread = rsi_1d - max(rsi_4h, rsi_1h)
        return f"⚠️ RSI 다이버전스 의심 (1D vs 4H/1H 격차 {spread:.0f})"
    if rsi_1d >= 90 and rsi_4h < 75 and rsi_1h < 75:
        return "⚠️ 단기 모멘텀 식음 — 진입 트리거 약함"
    return None


# ── 필터 체크리스트 ──────────────────────────────────────
def filter_chips(report: dict | None) -> str:
    if not report:
        return C.DIM + "(필터 미실행)" + C.RESET
    parts = []
    for r in report["results"]:
        name_short = r["name"].replace(" 필터", "")
        if r["passed"]:
            parts.append(f"{C.GREEN}✓{name_short}{C.RESET}")
        else:
            parts.append(f"{C.RED}✗{name_short}{C.RESET}")
    return " ".join(parts)


# ── 메인 카드 출력 ───────────────────────────────────────
def print_card(sig: dict) -> None:
    ctx = sig["context"]
    grade = sig["grade"]
    col = GRADE_COLOR.get(grade, "")
    sym = sig["symbol"].replace(":USDT", "")

    price = ctx["price"]
    upper = ctx["bb_upper_1d"]
    middle = ctx["bb_middle_1d"]
    lower = ctx["bb_lower_1d"]
    dev = (ctx["bb_dev_1d"] or 0) * 100
    pctl = ctx["bb_dev_pctl_1d"] or 0
    rsi_1d = ctx.get("rsi6_1d")
    rsi_4h = ctx.get("rsi6_4h")
    rsi_1h = ctx.get("rsi6_1h")
    funding = (ctx.get("funding_rate") or 0) * 100
    interval = ctx.get("funding_interval_h", 8)

    print()
    print(C.BOLD + "═" * 78 + C.RESET)
    print(f" {col} {grade:^3} {C.RESET}  {C.BOLD}{sym:<18}{C.RESET} "
          f"→ {sig['name']}  "
          f"{C.DIM}(권장 {sig['weight']*100:.2f}%){C.RESET}")
    print(C.BOLD + "─" * 78 + C.RESET)

    # BB 라인
    bb_bar = bb_position(price, upper, middle, lower)
    print(f" BB  {bb_bar}  "
          f"price={price:.6g}  upper={upper:.6g}")
    print(f"     이격: {dev_color(dev)}{dev:+6.2f}%{C.RESET} "
          f"(백분위 {pctl:5.1f}%) "
          f"{C.DIM}|  [={lower:.5g}, |={middle:.5g}, ]={upper:.5g}{C.RESET}")

    # RSI 멀티 TF
    print(f" RSI(6)   1D {gauge(rsi_1d or 0, 0, 100, 18)} {rsi_color(rsi_1d)}")
    print(f"          4H {gauge(rsi_4h or 0, 0, 100, 18)} {rsi_color(rsi_4h)}")
    print(f"          1H {gauge(rsi_1h or 0, 0, 100, 18)} {rsi_color(rsi_1h)}")

    div = divergence_warning(rsi_1d, rsi_4h, rsi_1h)
    if div:
        print(f"     {C.YELLOW + C.BOLD}{div}{C.RESET}")

    # 펀비
    f_col = funding_color(funding)
    hourly = funding / max(interval, 1)
    f_state = "음펀비" if funding < 0 else "양펀비"
    print(f" 펀비    {f_col}{funding:+8.4f}%{C.RESET} / {interval}h  "
          f"({f_state} · 1h환산 {hourly:+.5f}%)")

    # 필터
    report = sig.get("filter_report")
    print(f" 필터    {filter_chips(report)}")
    if report and not report["all_passed"]:
        for r in report["results"]:
            if not r["passed"]:
                print(f"         {C.RED}✗ {r['name']}: {r['detail']}{C.RESET}")


# ── 메인 ─────────────────────────────────────────────────
def latest_scan_file(out_dir: str = "output") -> str | None:
    files = sorted(glob.glob(os.path.join(out_dir, "scan_*.json")))
    return files[-1] if files else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path", nargs="?", help="JSON 파일 경로 (생략시 output/ 최신)")
    ap.add_argument("--min-grade", default="A",
                    choices=["S+", "S", "A", "B", "C+", "C"])
    ap.add_argument("--no-color", action="store_true")
    args = ap.parse_args()

    if args.no_color or not sys.stdout.isatty():
        # Windows cmd는 isatty여도 ANSI 미지원일 수 있음 — 사용자 옵션 우선
        if args.no_color:
            no_color()
        else:
            # Windows 10+ VT 활성화 시도
            if os.name == "nt":
                try:
                    import ctypes
                    k = ctypes.windll.kernel32
                    k.SetConsoleMode(k.GetStdHandle(-11), 7)
                except Exception:
                    no_color()

    path = args.path or latest_scan_file()
    if not path or not os.path.exists(path):
        print(f"{C.RED}스캔 JSON을 찾지 못함: {path}{C.RESET}")
        sys.exit(1)

    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    cfg = data.get("config", {})
    print(C.BOLD + C.CYAN + "═" * 78 + C.RESET)
    print(f"{C.BOLD}📊 데드킹 스캐너 결과 시각화{C.RESET}  "
          f"{C.DIM}({os.path.basename(path)}){C.RESET}")
    print(f"  거래소: {cfg.get('exchange')}  |  "
          f"BB({cfg.get('bb_period')},{cfg.get('bb_period')==22 and 2 or '?'})  "
          f"RSI({cfg.get('rsi_length')})  |  "
          f"굿쇼존 ≥ {cfg.get('good_short_pctl')}pctl  |  "
          f"OS ≥ {cfg.get('os_threshold',0)*100:.2g}%  |  "
          f"MAD ≥ {cfg.get('mad_os_threshold',0)*100:.2g}%")

    # 필터링 + 정렬
    min_rank = GRADE_RANK[args.min_grade]
    signals = [s for s in data["signals"]
               if GRADE_RANK.get(s["grade"], 99) <= min_rank]
    signals.sort(key=lambda s: (GRADE_RANK[s["grade"]],
                                -(s["context"].get("bb_dev_1d") or 0)))

    if not signals:
        print(f"{C.YELLOW}진입 가능 신호 ({args.min_grade} 등급 이상) 없음{C.RESET}")
        return

    # 등급별 집계 한 줄
    by_grade = {}
    for s in data["signals"]:
        by_grade.setdefault(s["grade"], []).append(s)
    tally_parts = []
    for g in ["S+", "S", "A", "B", "C+", "C"]:
        n = len(by_grade.get(g, []))
        if n:
            col = GRADE_COLOR.get(g, "")
            tally_parts.append(f"{col}{g}×{n}{C.RESET}")
    if tally_parts:
        print(f"  집계: {' · '.join(tally_parts)}")

    for s in signals:
        print_card(s)

    print()
    print(C.BOLD + "═" * 78 + C.RESET)
    print(f" 진입 후보 {len(signals)}개  "
          f"{C.DIM}(--min-grade {args.min_grade} 적용){C.RESET}")
    pass_cnt = sum(1 for s in signals
                   if s.get("filter_report") and s["filter_report"]["all_passed"])
    print(f" 그중 ALL PASS: {C.GREEN}{C.BOLD}{pass_cnt}개{C.RESET}")


if __name__ == "__main__":
    main()
