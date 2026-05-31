"""
운영자 신호 vs 우리 스캐너 결과 매칭 분석기 (Phase 1 후속).

입력:
  - output/operator_signals.jsonl   (telegram_listener.py 가 누적 기록)
  - output/scan_*.json              (scanner 가 15분마다 저장)

매칭 규칙:
  - 운영자 신호 timestamp 의 ±15분 안에 떨어진 scan_*.json 중
    timestamp 가 가장 가까운 것을 골라 같은 심볼의 등급/zone 을 비교
  - 윈도우 안에 scan 이 없으면 "no_scan" 으로 카운트

집계:
  - zone 일치율 (GoodShort vs Monitoring vs Deadking) — suffix 무시
  - grade 일치율 (S+/S/A/B/C+/C) — 일부만 등급화 가능
  - 일별 누적 + 등급별 분포

사용:
    python match_analyzer.py                     # 누적된 전체
    python match_analyzer.py --since 2026-05-24  # 특정 날짜부터
    python match_analyzer.py --window 15         # ±N 분 (기본 15)
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent
OUT_DIR = HERE / "output"
OPERATOR_JSONL = OUT_DIR / "operator_signals.jsonl"
OPERATOR_HISTORY_JSONL = OUT_DIR / "operator_history.jsonl"   # 과거 누적 (parse_operator_history.py 산물)


# 운영자 등급(텍스트) → zone 매핑
OPERATOR_ZONE = {
    "Monitoring": "MONITORING",
    "Monitoring OS": "MONITORING",
    "Monitoring MAD OS": "MONITORING",
    "Good short zone": "GOOD_SHORT",
    "Good short zone OS": "GOOD_SHORT",
    "Good short zone MAD OS": "GOOD_SHORT",
    "Deadking zone": "DEADKING",
    "Deadking zone OS": "DEADKING",
}

# 우리 등급 → zone (운영자 비교용 — match_operator.py 와 동일)
GRADE_TO_ZONE = {
    "S+": "GOOD_SHORT", "S": "GOOD_SHORT", "A": "GOOD_SHORT",
    "B": "MONITORING", "C+": "MONITORING", "C": "MONITORING",
    "NONE": "NONE",
}

# 운영자 등급(텍스트) → 우리 grade 코드 (suffix 까지 포함한 정밀 매핑)
OPERATOR_GRADE_TO_OURS = {
    "Good short zone MAD OS": "S+",
    "Good short zone OS":     "S",
    "Good short zone":        "A",
    "Monitoring MAD OS":      "B",
    "Monitoring OS":          "C+",
    "Monitoring":             "C",
}


def base_symbol(sym: Optional[str]) -> Optional[str]:
    """'BAN/USDT:USDT' 또는 'BAN/USDT' 또는 'BAN' → 'BAN'."""
    if not sym:
        return None
    return sym.split("/", 1)[0].upper()


def load_operator_history(path: Path = OPERATOR_HISTORY_JSONL) -> tuple[list[dict], list[dict]]:
    """과거 누적 운영자 history (HTML 익스포트 산물) 로드.
    Returns: (signals, system_events).
    """
    if not path.exists():
        return [], []
    sigs: list[dict] = []
    sys_events: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                rec = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if rec.get("kind") == "system":
                sys_events.append(rec)
            else:
                sigs.append(rec)
    return sigs, sys_events


def compute_symbol_priors(history_signals: list[dict]) -> dict[str, dict]:
    """
    종목별 운영자 신호 빈도 / 등급 분포 prior 를 계산.
    스캐너 우선순위 가중치 (출현 잦은 종목 = 단골 = 같은 패턴 반복 가능성 ↑) 산출용.

    Returns: {symbol: {"count": N, "grades": Counter, "weight": 0~1}}
        weight = log1p(count) 정규화 (가장 잦은 종목이 1.0)
    """
    import math
    by_sym = defaultdict(lambda: {"count": 0, "grades": Counter()})
    for rec in history_signals:
        sym = rec.get("symbol")
        if not sym:
            continue
        by_sym[sym]["count"] += 1
        g = rec.get("grade")
        if g:
            by_sym[sym]["grades"][g] += 1
    if not by_sym:
        return {}
    max_log = max(math.log1p(d["count"]) for d in by_sym.values())
    result: dict[str, dict] = {}
    for sym, d in by_sym.items():
        result[sym] = {
            "count": d["count"],
            "grades": dict(d["grades"]),
            "weight": math.log1p(d["count"]) / max_log if max_log > 0 else 0.0,
        }
    return result


def load_operator_signals(path: Path, since: Optional[datetime] = None) -> list[dict]:
    if not path.exists():
        print(f"❌ 운영자 신호 파일 없음: {path}", file=sys.stderr)
        return []
    out: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                rec = json.loads(ln)
            except json.JSONDecodeError:
                continue
            ts_str = rec.get("timestamp")
            if not ts_str:
                continue
            try:
                rec["_ts"] = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            except ValueError:
                continue
            if since and rec["_ts"] < since:
                continue
            out.append(rec)
    return out


def load_scan_index(out_dir: Path) -> list[tuple[datetime, Path]]:
    """모든 scan_*.json 의 (timestamp, path) 목록을 정렬해서 반환."""
    idx: list[tuple[datetime, Path]] = []
    for p in out_dir.glob("scan_*.json"):
        # 파일명 scan_YYYYMMDD_HHMMSS.json — UTC 로 가정
        stem = p.stem.replace("scan_", "")
        try:
            ts = datetime.strptime(stem, "%Y%m%d_%H%M%S").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        idx.append((ts, p))
    idx.sort(key=lambda x: x[0])
    return idx


def find_closest_scan(scan_idx: list[tuple[datetime, Path]], target: datetime,
                      window: timedelta) -> Optional[Path]:
    """target 시각의 ±window 안에서 가장 가까운 scan 파일 경로."""
    best: Optional[tuple[timedelta, Path]] = None
    for ts, p in scan_idx:
        delta = abs(ts - target)
        if delta > window:
            continue
        if best is None or delta < best[0]:
            best = (delta, p)
    return best[1] if best else None


def load_scan_signals(path: Path) -> dict[str, dict]:
    """scan_*.json 을 읽어 {base_symbol: signal_dict} 로 변환."""
    with open(path, encoding="utf-8") as f:
        payload = json.load(f)
    out = {}
    for s in payload.get("signals", []):
        sym = base_symbol(s.get("symbol"))
        if sym:
            out[sym] = s
    return out


# ============================================================
# 매칭
# ============================================================
def match(op_signals: list[dict], scan_idx: list[tuple[datetime, Path]],
          window_min: int) -> list[dict]:
    """각 운영자 신호별 매칭 결과 list."""
    window = timedelta(minutes=window_min)
    results = []
    scan_cache: dict[Path, dict[str, dict]] = {}

    for op in op_signals:
        sym = base_symbol(op.get("symbol"))
        op_grade = op.get("grade")
        op_zone = OPERATOR_ZONE.get(op_grade) if op_grade else None

        rec = {
            "op_timestamp": op["timestamp"],
            "op_symbol": sym,
            "op_grade": op_grade,
            "op_zone": op_zone,
            "scan_path": None,
            "scan_delta_sec": None,
            "our_grade": None,
            "our_zone": None,
            "zone_match": None,
            "grade_match": None,
            "status": "unknown",
        }

        if sym is None:
            rec["status"] = "op_symbol_unparsed"
            results.append(rec)
            continue
        if op_grade is None:
            rec["status"] = "op_grade_unparsed"
            results.append(rec)
            continue

        scan_path = find_closest_scan(scan_idx, op["_ts"], window)
        if scan_path is None:
            rec["status"] = "no_scan_in_window"
            results.append(rec)
            continue

        rec["scan_path"] = scan_path.name
        # delta 다시 계산 (로깅용)
        for ts, p in scan_idx:
            if p == scan_path:
                rec["scan_delta_sec"] = int((ts - op["_ts"]).total_seconds())
                break

        if scan_path not in scan_cache:
            scan_cache[scan_path] = load_scan_signals(scan_path)
        sigs = scan_cache[scan_path]

        our = sigs.get(sym)
        if our is None:
            # 스캔은 있는데 우리 결과에 그 종목이 없음 = 분류 NONE 으로 컷됨
            rec["our_grade"] = "NONE"
            rec["our_zone"] = "NONE"
            rec["zone_match"] = (op_zone == "NONE")
            rec["grade_match"] = False
            rec["status"] = "ours_filtered_out"
        else:
            our_grade = our.get("grade", "NONE")
            rec["our_grade"] = our_grade
            rec["our_zone"] = GRADE_TO_ZONE.get(our_grade, "?")
            rec["zone_match"] = (rec["our_zone"] == op_zone)
            rec["grade_match"] = (our_grade == OPERATOR_GRADE_TO_OURS.get(op_grade))
            rec["status"] = "matched"

        results.append(rec)

    return results


# ============================================================
# 출력
# ============================================================
def print_report(records: list[dict]) -> None:
    if not records:
        print("매칭할 운영자 신호가 없다.")
        return

    print("=" * 88)
    print(f"운영자 신호 vs 스캐너 결과 — 총 {len(records)}건")
    print("=" * 88)
    hdr = f"  {'시각':<19} {'심볼':<8} {'운영자':<22} {'우리':<5} {'zone':<6} {'결과'}"
    print(hdr)
    print("  " + "-" * 84)
    for r in records:
        ts = r["op_timestamp"][:19].replace("T", " ")
        mark = {
            "matched": "✅" if r["zone_match"] else "❌",
            "ours_filtered_out": "🚫",
            "no_scan_in_window": "⏳",
            "op_grade_unparsed": "❓",
            "op_symbol_unparsed": "❓",
        }.get(r["status"], "?")
        print(
            f"  {ts:<19} {(r['op_symbol'] or '?'):<8} "
            f"{(r['op_grade'] or '?'):<22} "
            f"{(r['our_grade'] or '-'):<5} "
            f"{(r['our_zone'] or '-'):<6} "
            f"{mark} {r['status']}"
        )

    # ── 전체 통계 ─────────────────────────────────────
    matched = [r for r in records if r["status"] == "matched"]
    zone_ok = sum(1 for r in matched if r["zone_match"])
    grade_ok = sum(1 for r in matched if r["grade_match"])
    no_scan = sum(1 for r in records if r["status"] == "no_scan_in_window")
    filtered = sum(1 for r in records if r["status"] == "ours_filtered_out")
    unparsed = sum(1 for r in records if r["status"].endswith("_unparsed"))

    print()
    print("─ 전체 통계 ─")
    print(f"  매칭 시도 (스캔 + 분류 결과 존재): {len(matched)} / {len(records)}")
    if matched:
        print(f"  zone 일치율  (운영자 zone == 우리 zone) : {zone_ok}/{len(matched)} = {zone_ok/len(matched)*100:.1f}%")
        print(f"  grade 일치율 (suffix 까지 정확히 일치) : {grade_ok}/{len(matched)} = {grade_ok/len(matched)*100:.1f}%")
    print(f"  스캔 윈도우 밖 (±15분 안에 스캔 없음): {no_scan}")
    print(f"  우리 분류 NONE 으로 컷됨            : {filtered}")
    print(f"  운영자 메시지 파싱 실패            : {unparsed}")

    # ── 일별 ──────────────────────────────────────────
    by_day: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        day = r["op_timestamp"][:10]
        by_day[day].append(r)

    print()
    print("─ 일별 매칭률 ─")
    print(f"  {'날짜':<12} {'운영자':<6} {'매칭':<6} {'zone일치':<10} {'grade일치':<10}")
    for day in sorted(by_day):
        recs = by_day[day]
        m = [r for r in recs if r["status"] == "matched"]
        z = sum(1 for r in m if r["zone_match"])
        g = sum(1 for r in m if r["grade_match"])
        zp = f"{z}/{len(m)} ({z/len(m)*100:.0f}%)" if m else "-"
        gp = f"{g}/{len(m)} ({g/len(m)*100:.0f}%)" if m else "-"
        print(f"  {day:<12} {len(recs):<6} {len(m):<6} {zp:<10} {gp:<10}")

    # ── 운영자 등급 분포 ───────────────────────────
    print()
    print("─ 운영자 등급 분포 ─")
    grade_count = Counter(r["op_grade"] for r in records if r["op_grade"])
    for g, c in grade_count.most_common():
        print(f"  {g:<24} {c}")


def print_history_summary(priors: dict[str, dict], system_events: list[dict]) -> None:
    if not priors:
        return
    print()
    print("=" * 88)
    print(f"📚 과거 운영자 history (operator_history.jsonl) — {len(priors)} 종목 누적")
    print("=" * 88)
    top = sorted(priors.items(), key=lambda kv: -kv[1]["count"])[:15]
    print(f"  {'심볼':<12} {'빈도':<5} {'가중치':<7} {'주요 등급'}")
    for sym, d in top:
        grades = ", ".join(f"{g}:{c}" for g, c in sorted(d["grades"].items(), key=lambda kv: -kv[1])[:3])
        print(f"  {sym:<12} {d['count']:<5} {d['weight']:.2f}   {grades}")
    if system_events:
        kinds = Counter(s["event"] for s in system_events)
        print(f"  ─ 시스템 이벤트: {dict(kinds)}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="YYYY-MM-DD 이후만 분석 (UTC)")
    ap.add_argument("--window", type=int, default=15, help="매칭 윈도우(±N 분, 기본 15)")
    ap.add_argument("--history-only", action="store_true",
                    help="실시간 매칭 없이 과거 운영자 history 통계만 출력")
    args = ap.parse_args()

    since = None
    if args.since:
        try:
            since = datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            print(f"--since 형식 오류: {args.since!r} (YYYY-MM-DD 필요)", file=sys.stderr)
            return 2

    # 과거 history (HTML 익스포트 파싱 산물) 로드 — 항상 출력
    hist_signals, hist_systems = load_operator_history()
    priors = compute_symbol_priors(hist_signals)
    print_history_summary(priors, hist_systems)

    if args.history_only:
        return 0

    op_signals = load_operator_signals(OPERATOR_JSONL, since=since)
    if not op_signals:
        print()
        print("운영자 실시간 신호 0건 (operator_signals.jsonl). "
              "telegram_listener.py 가 한 번이라도 메시지를 받았는지 확인.")
        return 0
    scan_idx = load_scan_index(OUT_DIR)
    if not scan_idx:
        print("scan_*.json 0개. 스캐너를 한 번이라도 돌렸는지 확인.")
        return 0

    records = match(op_signals, scan_idx, args.window)
    # 종목별 prior weight 첨부 (사후 분석/우선순위용)
    for r in records:
        sym = r.get("op_symbol")
        if sym and sym in priors:
            r["history_count"] = priors[sym]["count"]
            r["history_weight"] = priors[sym]["weight"]
        else:
            r["history_count"] = 0
            r["history_weight"] = 0.0
    print_report(records)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
