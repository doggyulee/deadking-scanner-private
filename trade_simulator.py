"""
검색기 신호 ↔ 강의 채널 매매 매칭 시뮬레이터.

목적:
  각 강의 채널 ACTION (## 비중 X% 정리) 마다, 그 직전 24시간 안에
  발사된 같은 종목의 검색기 신호 중 진입 시점을 추정하고 통계를 낸다.

진입 시점 추정 (직전 24h 안에서 우선순위 순):
  1. 첫 Good short zone OS / Good short zone MAD OS (실제 진입가 가까운 자리)
  2. 첫 Good short zone (확정 진입 자리)
  3. 첫 Monitoring MAD OS / Monitoring OS (모니터링이 격상된 자리)
  4. 첫 Monitoring (모니터링 첫 등장)

매칭 결과:
  - 진입 시각 / 종료 시각 / 보유 시간
  - 진입 시그널 등급, BB%, 5MA%, 태그
  - 익절 비중
  - 종목별 / 전체 통계

검증 케이스:
  BEAT (2026-05-24)
    진입: UTC 02:05 ≈ +07:00 09:05  Good short zone  BB+15% 5MA+32% SCAM
    종료: UTC 08:36 ≈ +07:00 15:36  ## 비중 50% 정리
    보유: ~6시간 31분
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent
OUT_DIR = HERE / "output"
OPERATOR_HISTORY = OUT_DIR / "operator_history.jsonl"
OPERATOR_TRADES = OUT_DIR / "operator_trades.jsonl"

# 진입 시그널 우선순위 (직전 24h 안에서 어느 등급을 첫 진입으로 채택?)
ENTRY_PRIORITY: list[list[str]] = [
    ["Good short zone OS", "Good short zone MAD OS"],
    ["Good short zone"],
    ["Monitoring MAD OS", "Monitoring OS"],
    ["Monitoring"],
]


@dataclass
class Match:
    symbol: str
    action_ts: str             # 종료 (## 비중 X% 정리) ISO
    weight_pct: int
    is_additional: bool
    entry_ts: Optional[str] = None
    entry_grade: Optional[str] = None
    entry_bb: Optional[float] = None
    entry_ma5: Optional[float] = None
    entry_tags: list[str] = field(default_factory=list)
    entry_priority_tier: Optional[int] = None  # ENTRY_PRIORITY 의 몇 번째 그룹에서 매칭됐나
    hold_seconds: Optional[int] = None         # 보유 시간 (초)

    @property
    def hold_hm(self) -> str:
        if self.hold_seconds is None:
            return "-"
        h, rem = divmod(self.hold_seconds, 3600)
        m, _ = divmod(rem, 60)
        return f"{h}h{m:02d}m"


# ============================================================
# 로딩
# ============================================================
def load_history() -> list[dict]:
    recs: list[dict] = []
    if not OPERATOR_HISTORY.exists():
        print(f"❌ {OPERATOR_HISTORY} 없음. parse_operator_history.py 먼저 실행.", file=sys.stderr)
        return recs
    with open(OPERATOR_HISTORY, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                r = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if r.get("kind") == "system":
                continue
            if not r.get("symbol") or not r.get("timestamp"):
                continue
            r["_ts"] = datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00"))
            recs.append(r)
    recs.sort(key=lambda r: r["_ts"])
    return recs


def load_actions() -> list[dict]:
    """ACTION (비중 X%) 만 추출 — ACTION_OUT 은 제외."""
    out: list[dict] = []
    if not OPERATOR_TRADES.exists():
        print(f"❌ {OPERATOR_TRADES} 없음. parse_leading_channel.py 먼저 실행.", file=sys.stderr)
        return out
    with open(OPERATOR_TRADES, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                r = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if r.get("kind") != "ACTION":
                continue
            r["_ts"] = datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00"))
            out.append(r)
    out.sort(key=lambda r: r["_ts"])
    return out


# ============================================================
# 매칭
# ============================================================
def find_entry(history: list[dict], symbol: str, action_ts: datetime,
               lookback_hours: int = 24) -> Optional[tuple[dict, int]]:
    """
    action_ts 기준 직전 lookback_hours 안에서, 같은 symbol 의 시그널 중
    ENTRY_PRIORITY 우선순위에 따른 첫(=가장 이른) 시그널을 반환.
    Returns: (signal_record, priority_tier) or None
    """
    cutoff = action_ts - timedelta(hours=lookback_hours)
    candidates = [
        r for r in history
        if r["symbol"] == symbol and cutoff <= r["_ts"] <= action_ts
    ]
    if not candidates:
        return None
    # 시간 오름차순
    candidates.sort(key=lambda r: r["_ts"])
    for tier, grades in enumerate(ENTRY_PRIORITY):
        for r in candidates:
            if r.get("grade") in grades:
                return r, tier
    return None


def match_all(history: list[dict], actions: list[dict],
              lookback_hours: int = 24) -> list[Match]:
    matches: list[Match] = []
    for a in actions:
        sym = a["symbol"]
        action_ts = a["_ts"]
        m = Match(
            symbol=sym,
            action_ts=a["timestamp"],
            weight_pct=int(a.get("weight_pct", 0)),
            is_additional=bool(a.get("is_additional", False)),
        )
        result = find_entry(history, sym, action_ts, lookback_hours)
        if result:
            entry, tier = result
            m.entry_ts = entry["timestamp"]
            m.entry_grade = entry.get("grade")
            m.entry_bb = entry.get("bb_pct")
            m.entry_ma5 = entry.get("ma5_pct")
            m.entry_tags = list(entry.get("tags") or [])
            m.entry_priority_tier = tier
            m.hold_seconds = int((action_ts - entry["_ts"]).total_seconds())
        matches.append(m)
    return matches


# ============================================================
# 통계 출력
# ============================================================
def fmt_local(ts_iso: str, hours: int = 7) -> str:
    """ISO UTC → '+HH 로컬' 표기. 강의 채널 export 가 +07 이라 기본 +07."""
    dt = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
    local = dt.astimezone(timezone(timedelta(hours=hours)))
    return local.strftime("%m-%d %H:%M")


def print_matches(matches: list[Match], local_offset: int = 7) -> None:
    print()
    print("=" * 102)
    print(f"매매 매칭 결과 ({len(matches)} 건) — 로컬 시각 = UTC+{local_offset:02d}")
    print("=" * 102)
    hdr = (
        f"  {'심볼':<6} {'진입(local)':<12} {'종료(local)':<12} {'보유':<7} "
        f"{'진입등급':<24} {'BB':>5} {'5MA':>5} {'비중':>5} {'태그'}"
    )
    print(hdr)
    print("  " + "-" * 98)
    for m in matches:
        entry_local = fmt_local(m.entry_ts, local_offset) if m.entry_ts else "(없음)"
        action_local = fmt_local(m.action_ts, local_offset)
        bb = f"{m.entry_bb:+.0f}" if m.entry_bb is not None else "  -"
        ma5 = f"{m.entry_ma5:+.0f}" if m.entry_ma5 is not None else "  -"
        tags = ",".join(m.entry_tags) if m.entry_tags else ""
        add = "+추가" if m.is_additional else "     "
        grade = m.entry_grade or "(매칭 없음)"
        print(f"  {m.symbol:<6} {entry_local:<12} {action_local:<12} {m.hold_hm:<7} "
              f"{grade:<24} {bb:>5} {ma5:>5} {m.weight_pct:>3}%{add[:5]} {tags}")


def print_stats(matches: list[Match]) -> None:
    matched = [m for m in matches if m.hold_seconds is not None]
    if not matched:
        print("\n매칭된 케이스 없음.")
        return

    print()
    print("=" * 78)
    print(f"📊 통계 — 매칭 {len(matched)}/{len(matches)}")
    print("=" * 78)

    # 평균 보유 시간
    holds = [m.hold_seconds for m in matched]
    avg = int(statistics.mean(holds))
    med = int(statistics.median(holds))
    mn = min(holds)
    mx = max(holds)

    def fmt(s):
        h, r = divmod(s, 3600)
        mm, _ = divmod(r, 60)
        return f"{h}h{mm:02d}m"

    print(f"  평균 보유 : {fmt(avg)}")
    print(f"  중앙값    : {fmt(med)}")
    print(f"  최단/최장 : {fmt(mn)} ~ {fmt(mx)}")

    # 익절 비중 분포
    pct_count = Counter(m.weight_pct for m in matches)
    print()
    print("  ─ 익절 비중 분포 ─")
    for p in sorted(pct_count, reverse=True):
        print(f"    {p:>3}% : {pct_count[p]}회")

    # 진입 시그널 등급 분포
    grade_count = Counter(m.entry_grade for m in matched)
    print()
    print("  ─ 진입 시그널 등급 분포 ─")
    for g, c in grade_count.most_common():
        print(f"    {g:<24} {c}")

    # 진입 우선순위 tier
    tier_count = Counter(m.entry_priority_tier for m in matched)
    tier_names = ["Good short zone OS/MAD OS", "Good short zone", "Monitoring OS/MAD OS", "Monitoring"]
    print()
    print("  ─ 진입 우선순위 tier ─")
    for t in sorted(tier_count):
        name = tier_names[t] if t is not None and t < len(tier_names) else "?"
        print(f"    [{t}] {name:<28} {tier_count[t]}회")

    # 종목별 평균 보유 시간
    by_sym: dict[str, list[int]] = defaultdict(list)
    for m in matched:
        by_sym[m.symbol].append(m.hold_seconds)
    print()
    print("  ─ 종목별 평균 보유 시간 ─")
    for sym in sorted(by_sym, key=lambda s: -len(by_sym[s])):
        holds = by_sym[sym]
        avg_h = int(statistics.mean(holds))
        print(f"    {sym:<6} 매매 {len(holds)}회  평균 {fmt(avg_h)}")

    # BB / 5MA 진입 조건 분포
    bbs = [m.entry_bb for m in matched if m.entry_bb is not None]
    ma5s = [m.entry_ma5 for m in matched if m.entry_ma5 is not None]
    if bbs:
        print()
        print("  ─ 진입 조건 분포 (전체 매칭) ─")
        print(f"    BB  p10/p50/p90 = {sorted(bbs)[max(0,int(0.1*len(bbs))-1)]:+.0f}% / "
              f"{sorted(bbs)[len(bbs)//2]:+.0f}% / "
              f"{sorted(bbs)[max(0,int(0.9*len(bbs))-1)]:+.0f}%")
    if ma5s:
        print(f"    5MA p10/p50/p90 = {sorted(ma5s)[max(0,int(0.1*len(ma5s))-1)]:+.0f}% / "
              f"{sorted(ma5s)[len(ma5s)//2]:+.0f}% / "
              f"{sorted(ma5s)[max(0,int(0.9*len(ma5s))-1)]:+.0f}%")

    # 태그 빈도 (SCAM/SCAM?/GAP)
    tag_count = Counter()
    for m in matched:
        for t in m.entry_tags:
            tag_count[t] += 1
    if tag_count:
        print()
        print("  ─ 진입 시 태그 분포 ─")
        for t, c in tag_count.most_common():
            print(f"    {t:<10} {c}")


def beat_case_check(matches: list[Match]) -> None:
    """BEAT 5/24 케이스 자동 검증."""
    print()
    print("=" * 78)
    print("🎯 BEAT 5/24 케이스 자동 매칭 검증")
    print("=" * 78)
    beat = [m for m in matches if m.symbol == "BEAT"]
    if not beat:
        print("  ❌ BEAT 매칭 없음")
        return
    m = beat[0]
    entry_local = fmt_local(m.entry_ts, 7) if m.entry_ts else "(없음)"
    action_local = fmt_local(m.action_ts, 7)
    print(f"  진입: {entry_local} (+07)  {m.entry_grade}  "
          f"BB {m.entry_bb:+.0f}%  5MA {m.entry_ma5:+.0f}%  태그={m.entry_tags}")
    print(f"  종료: {action_local} (+07)  ## 비중 {m.weight_pct}% 정리")
    print(f"  보유: {m.hold_hm}")
    exp_grade = "Good short zone"
    exp_pct = 50
    ok = (
        m.entry_grade == exp_grade
        and m.weight_pct == exp_pct
        and 6 * 3600 <= (m.hold_seconds or 0) <= 7 * 3600   # 6h~7h 범위
    )
    print(f"  자동 검증: {'✅ PASS' if ok else '❌ FAIL'} "
          f"(기대: 진입 {exp_grade}, 비중 {exp_pct}%, 보유 ~6h31m)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lookback", type=int, default=24,
                    help="액션 직전 N시간 안에서 진입 시그널 찾기 (기본 24)")
    ap.add_argument("--local-offset", type=int, default=7,
                    help="로컬 시각 출력 오프셋 (기본 +7, 텔레그램 export 기준)")
    ap.add_argument("--save", action="store_true",
                    help="output/trade_matches.jsonl 로 저장")
    args = ap.parse_args()

    history = load_history()
    actions = load_actions()
    if not history or not actions:
        return 1

    print(f"📚 운영자 history: {len(history)} 시그널")
    print(f"📚 강의 액션:    {len(actions)} 매매")

    matches = match_all(history, actions, lookback_hours=args.lookback)
    print_matches(matches, local_offset=args.local_offset)
    print_stats(matches)
    beat_case_check(matches)

    if args.save:
        path = OUT_DIR / "trade_matches.jsonl"
        with open(path, "w", encoding="utf-8") as f:
            for m in matches:
                d = asdict(m)
                f.write(json.dumps(d, ensure_ascii=False) + "\n")
        print(f"\n💾 저장: {path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
