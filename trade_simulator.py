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

# ── 사용자 실매매 임포트 (바이낸스 포지션 히스토리) ──────────
USER_TRADES_TXT = HERE / "binance_history_may.txt"
USER_TRADES_OUT = OUT_DIR / "user_solo_trades.jsonl"
USER_TZ = timezone(timedelta(hours=7))          # 바이낸스 export 로컬 = UTC+7
STOCK_SYMBOLS = {"EWY", "SPY"}                    # 주식 (나머지는 crypto)

# trigger 매칭 윈도우 (분)
LECTURE_BRIEF_WINDOW_MIN = 120     # ENTRY_BRIEF ±2h
LECTURE_ACTION_BEFORE_MIN = 48 * 60  # 운영자 ACTION(정리=청산) 전 진입을 동행으로 인정하는 최대 선행 시간
LECTURE_ACTION_AFTER_MIN = 120     # ACTION 직후 진입 허용
SCANNER_WINDOW_MIN = 30            # Good short zone ±30m

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


# ============================================================
# 사용자 실매매 임포트 + 분석 (작업 1~5)
# ============================================================
import re

_BLOCK_HEADER = re.compile(
    r"^(?P<symbol>[A-Z0-9]+)USDT\s+Perp\s+(?P<lev>\d+)x\s+Cross\s+(?P<side>Short|Long)\s+Closed",
    re.IGNORECASE,
)


def parse_binance_history(path: Path = USER_TRADES_TXT) -> list[dict]:
    """바이낸스 포지션 히스토리 텍스트 → raw 트레이드 dict 리스트.

    블록은 빈 줄로 구분. 각 블록:
      SYMBOLUSDT Perp 10x Cross Short Closed
      Opened:  YYYY-MM-DD HH:MM:SS
      Closed:  YYYY-MM-DD HH:MM:SS
      Realized PNL: +X.XX USDT
      ROI: +X.XX%
      Volume: N SYMBOL
      Entry Price: X
      Avg Close Price: X
    """
    if not path.exists():
        print(f"❌ {path} 없음.", file=sys.stderr)
        return []

    text = path.read_text(encoding="utf-8")
    # 빈 줄(공백 포함) 기준으로 블록 분할
    blocks = re.split(r"\n\s*\n", text.strip())
    trades: list[dict] = []

    def _num(s: str) -> float:
        return float(s.replace(",", "").strip())

    for blk in blocks:
        lines = [ln.strip() for ln in blk.splitlines() if ln.strip()]
        if not lines:
            continue
        m = _BLOCK_HEADER.match(lines[0])
        if not m:
            continue
        rec: dict = {
            "symbol": m.group("symbol").upper(),
            "leverage": int(m.group("lev")),
            "side": m.group("side").upper(),
        }
        for ln in lines[1:]:
            if ln.startswith("Opened:"):
                rec["_open_raw"] = ln.split("Opened:", 1)[1].strip()
            elif ln.startswith("Closed:"):
                rec["_close_raw"] = ln.split("Closed:", 1)[1].strip()
            elif ln.startswith("Realized PNL:"):
                rec["realized_pnl_usdt"] = _num(ln.split(":", 1)[1].replace("USDT", ""))
            elif ln.startswith("ROI:"):
                rec["roi_pct"] = _num(ln.split(":", 1)[1].replace("%", ""))
            elif ln.startswith("Volume:"):
                rec["volume"] = _num(ln.split(":", 1)[1].replace(rec["symbol"], ""))
            elif ln.startswith("Entry Price:"):
                rec["entry_price"] = _num(ln.split(":", 1)[1])
            elif ln.startswith("Avg Close Price:"):
                rec["close_price"] = _num(ln.split(":", 1)[1])

        if "_open_raw" not in rec or "_close_raw" not in rec:
            continue
        # 로컬(+07) aware datetime
        rec["_open_dt"] = datetime.strptime(rec["_open_raw"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=USER_TZ)
        rec["_close_dt"] = datetime.strptime(rec["_close_raw"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=USER_TZ)
        rec["hold_minutes"] = int(round((rec["_close_dt"] - rec["_open_dt"]).total_seconds() / 60))
        trades.append(rec)

    return trades


def load_lecture_entries() -> tuple[list[dict], list[dict]]:
    """강의 채널(operator_trades.jsonl)에서 진입 브리핑/액션 추출.

    Returns: (entry_briefs, actions)
      entry_briefs: ENTRY_BRIEF (운영자가 '진입하세요' 한 시그널)
      actions:      ACTION (## 비중 X% 정리 = 운영자 청산. 운영자가 그 종목을 보유했었다는 증거)
    """
    briefs: list[dict] = []
    actions: list[dict] = []
    if not OPERATOR_TRADES.exists():
        return briefs, actions
    with open(OPERATOR_TRADES, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                r = json.loads(ln)
            except json.JSONDecodeError:
                continue
            sym = r.get("symbol")
            ts = r.get("timestamp")
            if not sym or not ts:
                continue
            r["_ts"] = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if r.get("kind") == "ENTRY_BRIEF":
                briefs.append(r)
            elif r.get("kind") == "ACTION":
                actions.append(r)
    return briefs, actions


def load_scanner_signals() -> list[dict]:
    """검색기 'Good short zone' 시그널만 추출 (operator_history.jsonl)."""
    sigs: list[dict] = []
    if not OPERATOR_HISTORY.exists():
        return sigs
    with open(OPERATOR_HISTORY, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                r = json.loads(ln)
            except json.JSONDecodeError:
                continue
            grade = r.get("grade") or ""
            if not grade.startswith("Good short zone"):
                continue
            sym = r.get("symbol")
            ts = r.get("timestamp")
            if not sym or not ts:
                continue
            r["_ts"] = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            sigs.append(r)
    return sigs


def classify_trigger(open_dt: datetime, symbol: str,
                     briefs: list[dict], actions: list[dict],
                     scanner: list[dict]) -> dict:
    """우선순위: operator_lecture > scanner_signal > manual.

    반환: {trigger, ref_ts, time_diff_min, detail}
      time_diff_min: 사용자 진입 - 운영자 시그널 (음수=사용자가 먼저)
    """
    # 1) ENTRY_BRIEF ±2h
    best = None
    for b in briefs:
        if b["symbol"] != symbol:
            continue
        diff = (open_dt - b["_ts"]).total_seconds() / 60
        if abs(diff) <= LECTURE_BRIEF_WINDOW_MIN:
            if best is None or abs(diff) < abs(best["time_diff_min"]):
                best = {"trigger": "operator_lecture", "ref_ts": b["timestamp"],
                        "time_diff_min": diff, "detail": "ENTRY_BRIEF"}
    if best:
        return best

    # 2) 운영자가 정리(ACTION)한 종목 → 운영자 주도 트레이드. 진입이 ACTION 전(선행) 또는 직후.
    best = None
    for a in actions:
        if a["symbol"] != symbol:
            continue
        diff = (open_dt - a["_ts"]).total_seconds() / 60  # 음수면 ACTION(청산) 전에 진입
        if -LECTURE_ACTION_BEFORE_MIN <= diff <= LECTURE_ACTION_AFTER_MIN:
            if best is None or abs(diff) < abs(best["time_diff_min"]):
                best = {"trigger": "operator_lecture", "ref_ts": a["timestamp"],
                        "time_diff_min": diff, "detail": "ACTION(운영자 청산)"}
    if best:
        return best

    # 3) 검색기 Good short zone ±30m
    best = None
    for s in scanner:
        if s["symbol"] != symbol:
            continue
        diff = (open_dt - s["_ts"]).total_seconds() / 60
        if abs(diff) <= SCANNER_WINDOW_MIN:
            if best is None or abs(diff) < abs(best["time_diff_min"]):
                best = {"trigger": "scanner_signal", "ref_ts": s["timestamp"],
                        "time_diff_min": diff, "detail": f"Good short zone {s.get('grade')}"}
    if best:
        return best

    return {"trigger": "manual", "ref_ts": None, "time_diff_min": None, "detail": "백분위 알파"}


def build_user_records(trades: list[dict]) -> list[dict]:
    """raw 트레이드 → TRADE_COMPLETE 스키마 레코드 (trigger/asset_type 분류 포함)."""
    briefs, actions = load_lecture_entries()
    scanner = load_scanner_signals()
    records: list[dict] = []
    for t in sorted(trades, key=lambda r: r["_open_dt"]):
        cls = classify_trigger(t["_open_dt"], t["symbol"], briefs, actions, scanner)
        asset_type = "stock" if t["symbol"] in STOCK_SYMBOLS else "crypto"
        rec = {
            "timestamp_open": t["_open_dt"].isoformat(),
            "timestamp_close": t["_close_dt"].isoformat(),
            "kind": "TRADE_COMPLETE",
            "symbol": t["symbol"],
            "side": t["side"],
            "leverage": t["leverage"],
            "entry_price": t["entry_price"],
            "close_price": t["close_price"],
            "volume": t["volume"],
            "realized_pnl_usdt": round(t["realized_pnl_usdt"], 2),
            "roi_pct": round(t["roi_pct"], 2),
            "hold_minutes": t["hold_minutes"],
            "trigger": cls["trigger"],
            "asset_type": asset_type,
            "note": f"auto-classified · {cls['detail']}",
        }
        # 매칭 메타 (시뮬레이터 분석용 — JSONL 에는 저장 안 함)
        rec["_match_ref_ts"] = cls["ref_ts"]
        rec["_match_detail"] = cls["detail"]
        rec["_match_time_diff_min"] = (
            round(cls["time_diff_min"], 1) if cls["time_diff_min"] is not None else None
        )
        records.append(rec)
    return records


def write_user_jsonl(records: list[dict]) -> tuple[int, int, int]:
    """기존 비-TRADE_COMPLETE 라인 보존 + TRADE_COMPLETE 54건 교체 기록.

    Returns: (preserved, imported, total)
    """
    preserved: list[str] = []
    if USER_TRADES_OUT.exists():
        with open(USER_TRADES_OUT, encoding="utf-8") as f:
            for ln in f:
                s = ln.strip()
                if not s:
                    continue
                try:
                    r = json.loads(s)
                except json.JSONDecodeError:
                    preserved.append(s)
                    continue
                if r.get("kind") == "TRADE_COMPLETE":
                    continue  # 재실행 시 중복 방지 — 기존 임포트 제거 후 재기록
                preserved.append(s)

    # _match_* 같은 내부 필드는 빼고 깔끔하게 저장
    clean = []
    for r in records:
        clean.append({k: v for k, v in r.items() if not k.startswith("_")})

    with open(USER_TRADES_OUT, "w", encoding="utf-8") as f:
        for s in preserved:
            f.write(s + "\n")
        for r in clean:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    total = len(preserved) + len(clean)
    return len(preserved), len(clean), total


# ── 통계 (작업 3) ─────────────────────────────────────────
def _fmt_hold(minutes: int) -> str:
    h, m = divmod(int(minutes), 60)
    if h >= 24:
        d, hh = divmod(h, 24)
        return f"{d}d{hh}h"
    return f"{h}h{m:02d}m"


def print_user_stats(records: list[dict]) -> None:
    crypto = [r for r in records if r["asset_type"] == "crypto"]
    stock = [r for r in records if r["asset_type"] == "stock"]
    wins = [r for r in records if r["realized_pnl_usdt"] > 0]
    losses = [r for r in records if r["realized_pnl_usdt"] <= 0]
    total_pnl = sum(r["realized_pnl_usdt"] for r in records)
    avg_roi = statistics.mean(r["roi_pct"] for r in records)

    print()
    print("=" * 78)
    print(f"📊 사용자 실매매 통계 — {len(records)}건 (승 {len(wins)} / 패 {len(losses)})")
    print("=" * 78)
    print(f"  승률      : {len(wins)/len(records)*100:.1f}%")
    print(f"  총 실현손익: {total_pnl:+.2f} USDT")
    print(f"  평균 ROI  : {avg_roi:+.1f}%   (중앙값 {statistics.median(r['roi_pct'] for r in records):+.1f}%)")

    # 1) 종목별 매매 횟수 + 평균 ROI
    by_sym: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        by_sym[r["symbol"]].append(r)
    print()
    print("  ─ ① 종목별 매매 횟수 + 평균 ROI ─")
    for sym in sorted(by_sym, key=lambda s: (-len(by_sym[s]), s)):
        rs = by_sym[sym]
        avg = statistics.mean(x["roi_pct"] for x in rs)
        pnl = sum(x["realized_pnl_usdt"] for x in rs)
        print(f"    {sym:<9} {len(rs):>2}회  평균ROI {avg:+7.1f}%  손익 {pnl:+7.2f}")

    # 2) 보유 시간 분포
    buckets = [("<1h", 0, 60), ("1-6h", 60, 360), ("6-24h", 360, 1440),
               ("1-3d", 1440, 4320), ("3d+", 4320, 10**9)]
    print()
    print("  ─ ② 보유 시간 분포 ─")
    for name, lo, hi in buckets:
        n = sum(1 for r in records if lo <= r["hold_minutes"] < hi)
        bar = "█" * n
        print(f"    {name:<7} {n:>2}건 {bar}")

    # 3) Trigger별 승률
    print()
    print("  ─ ③ Trigger별 승률 / 평균ROI ─")
    for trig in ("operator_lecture", "scanner_signal", "manual"):
        rs = [r for r in records if r["trigger"] == trig]
        if not rs:
            print(f"    {trig:<18} 0건")
            continue
        w = sum(1 for r in rs if r["realized_pnl_usdt"] > 0)
        avg = statistics.mean(r["roi_pct"] for r in rs)
        print(f"    {trig:<18} {len(rs):>2}건  승률 {w/len(rs)*100:5.1f}%  평균ROI {avg:+7.1f}%")

    # 4) 시간대별 진입 빈도 (로컬 +07 기준)
    print()
    print("  ─ ④ 시간대별 진입 빈도 (+07 로컬) ─")
    hour_cnt = Counter(datetime.fromisoformat(r["timestamp_open"]).hour for r in records)
    peak = max(hour_cnt.values()) if hour_cnt else 0
    for h in range(24):
        n = hour_cnt.get(h, 0)
        if n:
            bar = "█" * n
            mark = "  ← 최다" if n == peak else ""
            print(f"    {h:02d}시  {n:>2} {bar}{mark}")

    # 5) 최고 ROI Top 5
    print()
    print("  ─ ⑤ 최고 ROI Top 5 ─")
    for r in sorted(records, key=lambda x: -x["roi_pct"])[:5]:
        print(f"    {r['symbol']:<9} ROI {r['roi_pct']:+8.1f}%  "
              f"{r['realized_pnl_usdt']:+7.2f} USDT  보유 {_fmt_hold(r['hold_minutes'])}  [{r['trigger']}]")

    # 6) 최저 ROI 손실 분석
    print()
    print("  ─ ⑥ 손실 트레이드 분석 ─")
    if not losses:
        print("    (손실 없음)")
    for r in sorted(losses, key=lambda x: x["roi_pct"]):
        local = datetime.fromisoformat(r["timestamp_open"]).strftime("%m-%d %H:%M")
        print(f"    {r['symbol']:<9} ROI {r['roi_pct']:+8.1f}%  {r['realized_pnl_usdt']:+6.2f} USDT  "
              f"보유 {_fmt_hold(r['hold_minutes']):>7}  진입 {local}  [{r['asset_type']}]")

    # 7) 자산 타입별
    print()
    print("  ─ ⑦ 자산 타입별: 코인 vs 주식 ─")
    for name, rs in (("crypto", crypto), ("stock", stock)):
        if not rs:
            continue
        w = sum(1 for r in rs if r["realized_pnl_usdt"] > 0)
        pnl = sum(r["realized_pnl_usdt"] for r in rs)
        avg = statistics.mean(r["roi_pct"] for r in rs)
        syms = ",".join(sorted({r["symbol"] for r in rs})) if name == "stock" else ""
        print(f"    {name:<7} {len(rs):>2}건  승률 {w/len(rs)*100:5.1f}%  평균ROI {avg:+7.1f}%  손익 {pnl:+8.2f}  {syms}")


# ── 텍스트 차트 (작업 5) ──────────────────────────────────
def print_user_charts(records: list[dict]) -> None:
    print()
    print("=" * 78)
    print("📈 텍스트 차트")
    print("=" * 78)

    # ROI 분포 히스토그램
    print("\n  ─ ROI 분포 히스토그램 ─")
    bins = [(-1e9, 0, "손실 <0"), (0, 30, "0~30%"), (30, 60, "30~60%"),
            (60, 100, "60~100%"), (100, 150, "100~150%"), (150, 1e9, "150%+")]
    for lo, hi, name in bins:
        n = sum(1 for r in records if lo <= r["roi_pct"] < hi)
        print(f"    {name:<10} {n:>2} {'█'*n}")

    # 종목별 누적 PnL (상위 + 하위)
    by_sym: dict[str, float] = defaultdict(float)
    for r in records:
        by_sym[r["symbol"]] += r["realized_pnl_usdt"]
    print("\n  ─ 종목별 누적 PnL (USDT) ─")
    ordered = sorted(by_sym.items(), key=lambda kv: -kv[1])
    mx = max(abs(v) for v in by_sym.values()) or 1
    for sym, pnl in ordered:
        blocks = int(round(abs(pnl) / mx * 40))
        bar = ("█" * blocks) if pnl >= 0 else ("▒" * blocks)
        print(f"    {sym:<9} {pnl:+8.2f} {bar}")

    # Trigger별 승률 막대
    print("\n  ─ Trigger별 승률 막대 ─")
    for trig in ("operator_lecture", "scanner_signal", "manual"):
        rs = [r for r in records if r["trigger"] == trig]
        if not rs:
            continue
        wr = sum(1 for r in rs if r["realized_pnl_usdt"] > 0) / len(rs) * 100
        blocks = int(round(wr / 100 * 30))
        print(f"    {trig:<18} {wr:5.1f}% {'█'*blocks}{'░'*(30-blocks)} (n={len(rs)})")


# ── 시뮬레이터: 운영자 동행 분석 (작업 4) ─────────────────
def print_simulator_analysis(records: list[dict]) -> None:
    print()
    print("=" * 78)
    print("🤝 실매매 ↔ 운영자 시그널 매칭 분석")
    print("=" * 78)

    lecture = [r for r in records if r["trigger"] == "operator_lecture"]
    scanner = [r for r in records if r["trigger"] == "scanner_signal"]
    manual = [r for r in records if r["trigger"] == "manual"]

    def _timing(rs: list[dict]) -> None:
        """진입-시그널 시간차 (ENTRY_BRIEF / 스캐너처럼 '진입 신호' 매칭에만 의미)."""
        diffs = [r["_match_time_diff_min"] for r in rs if r.get("_match_time_diff_min") is not None]
        if not diffs:
            return
        avg_diff = statistics.mean(diffs)
        faster = sum(1 for d in diffs if d < 0)
        direction = "빨리" if avg_diff < 0 else "늦게"
        print(f"    평균 시간차: {abs(avg_diff):.0f}분 {direction} "
              f"(운영자보다 먼저 {faster}건 / 늦게 {len(diffs)-faster}건)")

    # 운영자 동행: ENTRY_BRIEF(진입 브리핑 동행) vs ACTION(운영자 보유종목 동행) 분리
    brief = [r for r in lecture if r.get("_match_detail") == "ENTRY_BRIEF"]
    held = [r for r in lecture if r.get("_match_detail") != "ENTRY_BRIEF"]
    print(f"\n  운영자 동행 (operator_lecture): {len(lecture)}건")
    if lecture:
        print(f"    평균 ROI : {statistics.mean(r['roi_pct'] for r in lecture):+.1f}%")
    if brief:
        print(f"    ├ 진입 브리핑 동행(ENTRY_BRIEF): {len(brief)}건 "
              f"평균ROI {statistics.mean(r['roi_pct'] for r in brief):+.1f}%")
        _timing(brief)
    if held:
        print(f"    └ 운영자 보유종목 동행(청산 ACTION 매칭): {len(held)}건 "
              f"평균ROI {statistics.mean(r['roi_pct'] for r in held):+.1f}%")

    print(f"\n  스캐너 신호 (scanner_signal): {len(scanner)}건")
    if scanner:
        print(f"    평균 ROI : {statistics.mean(r['roi_pct'] for r in scanner):+.1f}%")
        _timing(scanner)

    print(f"\n  본인 분석 (manual / 알파): {len(manual)}건")
    if manual:
        print(f"    평균 ROI : {statistics.mean(r['roi_pct'] for r in manual):+.1f}%")

    # 알파: 운영자/검색기 매칭 없이 본인이 잡은 종목
    if manual:
        alpha_syms: dict[str, list[dict]] = defaultdict(list)
        for r in manual:
            alpha_syms[r["symbol"]].append(r)
        print("\n  💡 발견된 알파 종목 (운영자 시그널 매칭 없이 본인 진입):")
        for sym in sorted(alpha_syms, key=lambda s: -statistics.mean(x["roi_pct"] for x in alpha_syms[s])):
            rs = alpha_syms[sym]
            avg = statistics.mean(r["roi_pct"] for r in rs)
            pnl = sum(r["realized_pnl_usdt"] for r in rs)
            print(f"    {sym:<9} {len(rs)}회  평균ROI {avg:+7.1f}%  손익 {pnl:+7.2f}")


def run_user_import(save: bool = True) -> int:
    trades = parse_binance_history()
    if not trades:
        print("❌ 파싱된 트레이드 없음.", file=sys.stderr)
        return 1
    records = build_user_records(trades)

    print(f"✅ 파싱 완료: {len(records)}건 변환")
    if save:
        preserved, imported, total = write_user_jsonl(records)
        print(f"💾 {USER_TRADES_OUT.name}: 기존 보존 {preserved}줄 + 임포트 {imported}줄 = 총 {total}줄")

    # trigger 분류 요약
    tc = Counter(r["trigger"] for r in records)
    print(f"🏷  trigger 분류: 운영자동행 {tc['operator_lecture']} / "
          f"스캐너 {tc['scanner_signal']} / 본인분석 {tc['manual']}")

    print_user_stats(records)
    print_user_charts(records)
    print_simulator_analysis(records)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lookback", type=int, default=24,
                    help="액션 직전 N시간 안에서 진입 시그널 찾기 (기본 24)")
    ap.add_argument("--local-offset", type=int, default=7,
                    help="로컬 시각 출력 오프셋 (기본 +7, 텔레그램 export 기준)")
    ap.add_argument("--save", action="store_true",
                    help="output/trade_matches.jsonl 로 저장")
    ap.add_argument("--user", action="store_true",
                    help="바이낸스 실매매 히스토리(binance_history_may.txt) 임포트 + 분석")
    ap.add_argument("--no-write", action="store_true",
                    help="--user 시 JSONL 쓰지 않고 분석만 출력")
    args = ap.parse_args()

    if args.user:
        return run_user_import(save=not args.no_write)

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
