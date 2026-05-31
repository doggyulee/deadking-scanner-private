"""
메인 스캐너
- 심볼 목록 가져오기
- 각 심볼에 대해 멀티 TF OHLCV + 펀비 페치
- 지표 계산
- 6등급 분류 + 7가지 PASS 필터
- 결과 출력 / JSON 저장 / 텔레그램 알림
"""
from __future__ import annotations

import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import datetime, timezone, timedelta

import pandas as pd

import config
import exchange as ex_mod
import indicators as ind
import filters as flt
import signal_history as sh
import divergence_tracker as dt
from classifier import (
    Signal,
    SignalContext,
    classify,
    classify_operator,
    classify_pctl,
    grade_at_least,
    GRADE_ORDER,
    GRADE_EMOJI,
)
import telegram_notifier as tg


log = logging.getLogger(__name__)


# ============================================================
# 황금 시간대 (KST 09:00~10:00 = UTC 00:00~01:00)
# 일봉 마감 직후라 일봉 BB/5MA 가 막 확정된 시점.
# 운영자 채널도 이 시간대에 신호 발사 빈도가 가장 높음.
# ============================================================
KST = timezone(timedelta(hours=9))
GOLDEN_HOUR_KST_START = 9        # KST 09:00
GOLDEN_HOUR_KST_END = 10         # KST 10:00 (배타)


def is_golden_hour(now_utc: datetime | None = None) -> bool:
    """현재 시각이 KST 09:00~10:00 안에 있는가."""
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    kst_hour = now_utc.astimezone(KST).hour
    return GOLDEN_HOUR_KST_START <= kst_hour < GOLDEN_HOUR_KST_END


# ============================================================
# 운영자 history / 매매 통계 캐시 (출력 강화용)
# ============================================================
_OP_STATS_CACHE: dict | None = None


def _load_op_stats() -> dict:
    """심볼 → 운영자 history 빈도 + 강의 채널 매매 통계 로드.
    파일이 없거나 빈 케이스도 안전 처리.
    Returns: {
        "history_count": {sym: int},
        "trade_count":   {sym: int},
        "avg_entry":     {sym: (bb_mean, ma5_mean)},
        "total_trades":  int,
    }
    """
    import json as _json
    from pathlib import Path as _Path
    out_dir = _Path(config.OUTPUT_DIR)
    stats = {"history_count": {}, "trade_count": {}, "avg_entry": {}, "total_trades": 0}

    # operator_history — 종목 출현 빈도
    hp = out_dir / "operator_history.jsonl"
    if hp.exists():
        cnt: dict[str, int] = {}
        with open(hp, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln: continue
                try:
                    r = _json.loads(ln)
                except _json.JSONDecodeError:
                    continue
                sym = r.get("symbol")
                if r.get("kind") == "system" or not sym: continue
                cnt[sym] = cnt.get(sym, 0) + 1
        stats["history_count"] = cnt

    # operator_trades — 강의 채널 매매 (kind=ACTION 만)
    tp = out_dir / "operator_trades.jsonl"
    if tp.exists():
        tc: dict[str, int] = {}
        with open(tp, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln: continue
                try:
                    r = _json.loads(ln)
                except _json.JSONDecodeError:
                    continue
                if r.get("kind") != "ACTION": continue
                sym = r.get("symbol")
                if sym:
                    tc[sym] = tc.get(sym, 0) + 1
        stats["trade_count"] = tc
        stats["total_trades"] = sum(tc.values())

    # trade_matches — 종목별 평균 진입 BB / 5MA
    mp = out_dir / "trade_matches.jsonl"
    if mp.exists():
        by_sym: dict[str, list[tuple[float, float]]] = {}
        with open(mp, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln: continue
                try:
                    r = _json.loads(ln)
                except _json.JSONDecodeError:
                    continue
                sym = r.get("symbol")
                bb = r.get("entry_bb")
                ma = r.get("entry_ma5")
                if sym is None or bb is None or ma is None: continue
                by_sym.setdefault(sym, []).append((float(bb), float(ma)))
        avg = {}
        for sym, pairs in by_sym.items():
            bb_mean = sum(p[0] for p in pairs) / len(pairs)
            ma_mean = sum(p[1] for p in pairs) / len(pairs)
            avg[sym] = (bb_mean, ma_mean)
        stats["avg_entry"] = avg
    return stats


def get_op_stats() -> dict:
    global _OP_STATS_CACHE
    if _OP_STATS_CACHE is None:
        _OP_STATS_CACHE = _load_op_stats()
    return _OP_STATS_CACHE


# ============================================================
# 심볼 1개 분석
# ============================================================
def analyze_symbol(
    exchange,
    symbol_info: dict,
    market_meta: dict | None = None,
    history: dict | None = None,
    tracker: dict | None = None,
) -> tuple[Signal | None, flt.FilterReport | None]:
    """
    단일 심볼에 대한 전체 분석 파이프라인.

    Returns: (Signal, FilterReport) — 분류 실패 시 (None, None)
    """
    symbol = symbol_info["symbol"]

    # ─────────────────────────────────────
    # 0) Deadking 블랙리스트 — 분석 전 단락
    # ─────────────────────────────────────
    if flt.is_deadking_blacklisted(symbol):
        log.info(f"⛔ {symbol}: Deadking 블랙리스트 (자동 NONE 처리)")
        return None, None

    try:
        # ─────────────────────────────────────
        # 1) OHLCV 페치 (1D / 4H / 1H)
        # ─────────────────────────────────────
        ohlcv = ex_mod.fetch_multi_tf(exchange, symbol)
        daily_raw = ohlcv.get(config.TF_DAILY)
        h4_raw = ohlcv.get(config.TF_4H)
        h1_raw = ohlcv.get(config.TF_1H)

        if not daily_raw or len(daily_raw) < config.BB_PERIOD + 5:
            log.debug(f"{symbol}: 일봉 데이터 부족")
            return None, None

        daily_df = ind.ohlcv_to_df(daily_raw)
        h4_df = ind.ohlcv_to_df(h4_raw) if h4_raw else None
        h1_df = ind.ohlcv_to_df(h1_raw) if h1_raw else None

        price = float(daily_df["close"].iloc[-1])

        # ─────────────────────────────────────
        # 2) 지표 계산
        # ─────────────────────────────────────
        upper, middle, lower = ind.bollinger_bands(
            daily_df["close"], config.BB_PERIOD, config.BB_STD
        )
        bb_upper = float(upper.iloc[-1])
        bb_middle = float(middle.iloc[-1])
        bb_lower = float(lower.iloc[-1])
        dev = ind.bb_deviation(price, bb_upper)

        # 30일 분포 백분위
        dev_series = ind.bb_deviation_series(daily_df["close"], config.BB_PERIOD, config.BB_STD)
        recent_devs = dev_series.tail(config.HISTORICAL_DEV_DAYS)
        pctl = ind.deviation_percentile(recent_devs, dev)

        # 5MA
        ma5 = ind.sma(daily_df["close"], config.MA_FAST).iloc[-1]
        ma5_val = float(ma5) if not pd.isna(ma5) else None
        ma5_dev = ind.ma_deviation(price, ma5_val) if ma5_val else None

        # RSI (각 TF)
        rsi_1d = float(ind.rsi(daily_df["close"], config.RSI_LENGTH).iloc[-1])
        rsi_4h = (
            float(ind.rsi(h4_df["close"], config.RSI_LENGTH).iloc[-1])
            if h4_df is not None and len(h4_df) > config.RSI_LENGTH else None
        )
        rsi_1h = (
            float(ind.rsi(h1_df["close"], config.RSI_LENGTH).iloc[-1])
            if h1_df is not None and len(h1_df) > config.RSI_LENGTH else None
        )

        # ─────────────────────────────────────
        # 3) 펀비
        # ─────────────────────────────────────
        funding_info = ex_mod.fetch_funding_rate(exchange, symbol)

        # ─────────────────────────────────────
        # 4) 컨텍스트 빌드 + 분류
        # ─────────────────────────────────────
        ctx = SignalContext(
            symbol=symbol,
            price=price,
            bb_upper_1d=bb_upper,
            bb_middle_1d=bb_middle,
            bb_lower_1d=bb_lower,
            bb_dev_1d=dev,
            bb_dev_pctl_1d=pctl,
            rsi6_1d=rsi_1d,
            rsi6_4h=rsi_4h,
            rsi6_1h=rsi_1h,
            ma5_1d=ma5_val,
            ma5_deviation=ma5_dev,
            volume_24h_usd=symbol_info.get("volume_usd"),
            funding_rate=(funding_info or {}).get("rate"),
            funding_interval_h=(funding_info or {}).get("interval_hours"),
        )

        # history suffix 계산: 운영자 모드에서 OS/MAD OS 격상 판단
        history_suffix = ""
        if history is not None:
            # 어떤 모드든 일단 사전 분류해서 신호가 있을 때만 history 갱신
            pre = classify_operator(ctx) if config.USE_OPERATOR_THRESHOLDS else classify_pctl(ctx)
            if pre.grade != "NONE":
                delta = sh.update(history, symbol, price)
                history_suffix = delta.suffix

        signal = classify(ctx, history_suffix=history_suffix)

        # GAP 감지 — 전일 종가 vs 당일 시가 3% 초과 시 'GAP' 태그
        is_gap, gap_pct = ind.detect_price_gap(daily_df, threshold=0.03)
        if is_gap:
            signal.tags.append("GAP")
            signal.gap_pct = gap_pct

        # OP_TRADED — 강의 채널에서 운영자가 실제 진입한 검증 자리
        if flt.is_operator_traded(symbol):
            signal.tags.append("OP_TRADED")

        # 다이버전스 등록 / 후속 관측
        if tracker is not None:
            spread = dt.detect(rsi_1d, rsi_4h, rsi_1h)
            if spread is not None:
                # 신호가 살아있을 때만 (NONE이면 그냥 관측만 채움)
                dt.register_or_update(
                    tracker, symbol, rsi_1d, rsi_4h, rsi_1h, spread, price, dev,
                )
            else:
                # 다이버전스 미감지여도 기존 추적 중인 레코드는 관측 채움
                from datetime import datetime, timezone, timedelta
                now = datetime.now(timezone.utc)
                cutoff = now - timedelta(hours=26)
                for k, rec in tracker.items():
                    if rec.get("symbol") != symbol or rec.get("resolved"):
                        continue
                    try:
                        t = datetime.fromisoformat(rec["detected_at"])
                    except (KeyError, ValueError):
                        continue
                    if t >= cutoff:
                        dt._attach_observation(rec, now, price, dev)
                        if not dt._pending_points(rec):
                            rec["resolved"] = True
                        break

        # ─────────────────────────────────────
        # 5) PASS 필터 (NONE이 아닐 때만 실행)
        # ─────────────────────────────────────
        if signal.grade == "NONE":
            return signal, None

        ath = ex_mod.estimate_ath_from_klines(daily_df)
        recent_max = ex_mod.recent_high(daily_df, config.LOOKBACK_HIGH_DAYS)

        report = flt.run_all_filters(
            symbol=symbol,
            price=price,
            ath=ath,
            recent_max=recent_max,
            daily_df=daily_df,
            funding_info=funding_info,
            deviation_pctl=pctl,
            deviation_abs=dev,
            distance_to_upper_cluster=None,  # Coinglass 연동시 채울 자리
            symbol_meta=market_meta or {},
        )
        return signal, report

    except Exception as e:
        log.exception(f"{symbol} 분석 실패: {e}")
        return None, None


# ============================================================
# 전체 스캔
# ============================================================
def scan_once() -> list[tuple[Signal, flt.FilterReport | None]]:
    """1회 전체 스캔."""
    started = time.time()
    scan_start_utc = datetime.now(timezone.utc)
    golden = is_golden_hour(scan_start_utc)
    log.info("=" * 60)
    log.info(f"🔍 데드킹 스캐너 시작 [{datetime.now().isoformat(timespec='seconds')}]")
    if golden:
        log.info("🌅 일봉 마감 직후 황금 시간대 (KST 09-10) — 신호 우선순위 ↑")
    log.info("=" * 60)

    exchange = ex_mod.make_exchange()
    symbols = ex_mod.fetch_usdt_perp_symbols(exchange)

    # market metadata 캐싱
    markets = exchange.markets or {}

    # 신호 history 로드 (운영자 OS/MAD OS 시간 추적용)
    history = sh.prune_expired(sh.load())
    log.info(f"📜 신호 history: {len(history)}개 종목 추적 중")

    # 다이버전스 트래커 로드 (모든 종목 후속 관측용 — A 등급 무관)
    tracker = dt.load()
    log.info(f"🧬 다이버전스 트래커: {len(tracker)}건 추적 중")

    results: list[tuple[Signal, flt.FilterReport | None]] = []
    completed = 0

    # 동시 처리
    with ThreadPoolExecutor(max_workers=config.CONCURRENT_REQUESTS) as pool:
        futures = {
            pool.submit(analyze_symbol, exchange, s, markets.get(s["symbol"]), history, tracker): s
            for s in symbols
        }
        for fut in as_completed(futures):
            s = futures[fut]
            completed += 1
            try:
                signal, report = fut.result()
                if signal is None:
                    continue
                results.append((signal, report))
                if signal.is_entry:
                    log.info(
                        f"[{completed}/{len(symbols)}] {s['symbol']}: "
                        f"{GRADE_EMOJI.get(signal.grade, '')} {signal.grade} ({signal.name})"
                    )
            except Exception as e:
                log.warning(f"{s['symbol']} 처리 실패: {e}")

    # 황금 시간대 스캔이면 모든 살아있는 신호에 태그
    if golden:
        for sig, _ in results:
            if sig.grade != "NONE":
                sig.tags.append("GOLDEN_HOUR")

    elapsed = time.time() - started
    log.info(f"✅ 스캔 완료: {len(results)}개 분석, {elapsed:.1f}초")

    # history 저장
    sh.save(history)
    dt.save(tracker)

    # 다이버전스 통계 출력
    dt.print_summary(tracker)
    return results


# ============================================================
# 결과 정렬 / 필터링
# ============================================================
def filter_actionable(
    results: list[tuple[Signal, flt.FilterReport | None]],
    min_grade: str = "A",
    require_filter_pass: bool = True,
) -> list[tuple[Signal, flt.FilterReport | None]]:
    """진입 가능한 시그널만 추출."""
    out = []
    for sig, rep in results:
        if not grade_at_least(sig.grade, min_grade):
            continue
        if require_filter_pass and rep is not None and not rep.all_passed:
            continue
        out.append((sig, rep))
    # 등급 우선 + 이격도 큰 순
    out.sort(key=lambda x: (GRADE_ORDER.index(x[0].grade), -x[0].context.bb_dev_1d))
    return out


# ============================================================
# 출력
# ============================================================
_OP_NAME_KR = {
    "S+": "Good short zone MAD OS", "S": "Good short zone OS", "A": "Good short zone",
    "B": "Monitoring MAD OS", "C+": "Monitoring OS", "C": "Monitoring",
}


def _operator_line(sig: Signal) -> str:
    """운영자 텔레그램 채널 형식 매칭: '🔥 Good short zone | BEAT/USDT, BB +29%, 5MA +51% (GAP)'."""
    ctx = sig.context
    g = sig.op_grade or "—"
    name = _OP_NAME_KR.get(g, "신호 없음")
    bb = (ctx.bb_dev_1d or 0) * 100
    ma = (ctx.ma5_deviation or 0) * 100
    emoji = "🔥" if g in ("S+", "S", "A") else ("👀" if g in ("B", "C+", "C") else "—")
    sym = sig.symbol.replace(":USDT", "")
    tag_str = f"  ({', '.join(sig.tags)})" if sig.tags else ""
    return f"  {emoji} {name:<24} | {sym:<14}  BB {bb:+6.1f}%  5MA {ma:+6.1f}%{tag_str}"


def print_summary(results: list[tuple[Signal, flt.FilterReport | None]]) -> None:
    """콘솔 요약 출력 (운영자 모드 + 백분위 모드 둘 다)."""
    by_grade = {}
    for sig, _ in results:
        by_grade.setdefault(sig.grade, []).append(sig)

    print()
    print("=" * 78)
    print(f"📊 등급별 집계 (메인 = {'운영자 절대' if config.USE_OPERATOR_THRESHOLDS else '백분위'} 모드)")
    print("=" * 78)
    for grade in GRADE_ORDER:
        items = by_grade.get(grade, [])
        if items:
            print(f"  {GRADE_EMOJI[grade]:<6} {grade:<3} ({len(items):>3}개)  "
                  f"{', '.join(s.symbol.replace(':USDT','') for s in items[:8])}")

    # 운영자 채널 형식 — 진입 등급(A 이상)
    op_entries = [s for s, _ in results
                  if s.op_grade and grade_at_least(s.op_grade, "A")]
    if op_entries:
        op_entries.sort(key=lambda s: GRADE_ORDER.index(s.op_grade))
        print()
        print("=" * 78)
        print("📡 운영자 텔레그램 형식 (op_grade ≥ A)")
        print("=" * 78)
        for s in op_entries:
            print(_operator_line(s))

    # 백분위 모드 결과
    pctl_entries = [s for s, _ in results
                    if s.pctl_grade and grade_at_least(s.pctl_grade, "A")]
    if pctl_entries:
        pctl_entries.sort(key=lambda s: GRADE_ORDER.index(s.pctl_grade))
        print()
        print("=" * 78)
        print("📊 백분위 모드 (pctl_grade ≥ A) — 스캐너 알파")
        print("=" * 78)
        for s in pctl_entries:
            ctx = s.context
            sym = s.symbol.replace(":USDT", "")
            print(f"  {GRADE_EMOJI[s.pctl_grade]:<5} [{s.pctl_grade:<2}] {sym:<14}  "
                  f"BB {(ctx.bb_dev_1d or 0)*100:+6.1f}%  pctl {ctx.bb_dev_pctl_1d:5.1f}%")

    # 모드 간 불일치 = 알파 신호
    alpha = [s for s, _ in results
             if s.op_grade and s.pctl_grade and s.op_grade != s.pctl_grade
             and (grade_at_least(s.op_grade, "A") or grade_at_least(s.pctl_grade, "A"))]
    if alpha:
        print()
        print("=" * 78)
        print("🧬 모드 간 불일치 — 너만의 알파 / 운영자 누락 후보")
        print("=" * 78)
        for s in alpha:
            sym = s.symbol.replace(":USDT", "")
            print(f"  {sym:<14}  운영자={s.op_grade:<3}  /  백분위={s.pctl_grade:<3}")

    # 진입 후보 상세
    actionable = filter_actionable(results, min_grade="A", require_filter_pass=False)
    if not actionable:
        print()
        print("⚠️  진입 가능 신호 (A 등급 이상) 없음")
        return

    print()
    print("=" * 78)
    print("🎯 진입 후보 상세 — 두 모드 매핑 + 필터")
    print("=" * 78)
    op_stats = get_op_stats()
    for sig, rep in actionable:
        ctx = sig.context
        emoji = GRADE_EMOJI[sig.grade]
        sym = sig.symbol.replace(":USDT", "")
        base = sym.split("/", 1)[0]
        tag_str = f"  [{', '.join(sig.tags)}]" if sig.tags else ""
        print()
        print(f"{emoji} [{sig.grade}] {sym}  →  {sig.name}  (mode={sig.mode}){tag_str}")
        if sig.gap_pct is not None:
            print(f"   📊 GAP: 전일 종가→당일 시가 {sig.gap_pct*100:+.2f}%")

        # 운영자 매매 이력 / 검색기 출현 빈도 / 평균 진입 조건
        op_trade_count = op_stats["trade_count"].get(base, 0)
        op_hist_count = op_stats["history_count"].get(base, 0)
        total_trades = op_stats.get("total_trades", 0) or 0
        if op_trade_count > 0:
            print(f"   ✅ 운영자 매매 이력: 강의 채널 총 {total_trades}건 중 {op_trade_count}건 = {base}")
        if op_hist_count > 0:
            print(f"   📚 검색기 출현: {op_hist_count}회 (운영자 채널 누적)")
        avg = op_stats["avg_entry"].get(base)
        if avg is not None:
            avg_bb, avg_ma = avg
            cur_bb = (ctx.bb_dev_1d or 0) * 100
            cur_ma = (ctx.ma5_deviation or 0) * 100
            db = cur_bb - avg_bb
            dm = cur_ma - avg_ma
            print(f"   📐 평균 진입 조건: BB +{avg_bb:.0f}% / 5MA +{avg_ma:.0f}%  "
                  f"(현재 vs 평균: BB {db:+.0f}p, 5MA {dm:+.0f}p)")

        print(f"   가격: {ctx.price:.6g}  |  권장비중: {sig.weight*100:.2f}%")
        print(f"   BB {(ctx.bb_dev_1d or 0)*100:+.2f}%  |  5MA {(ctx.ma5_deviation or 0)*100:+.2f}%  "
              f"|  pctl {ctx.bb_dev_pctl_1d:.0f}%")
        print(f"   ─ 운영자: {sig.op_grade or '—':<3}  /  백분위: {sig.pctl_grade or '—':<3}  "
              f"{'🧬 알파' if sig.is_alpha else ''}")

        rsi_str = []
        if ctx.rsi6_1d is not None: rsi_str.append(f"1D={ctx.rsi6_1d:.1f}")
        if ctx.rsi6_4h is not None: rsi_str.append(f"4H={ctx.rsi6_4h:.1f}")
        if ctx.rsi6_1h is not None: rsi_str.append(f"1H={ctx.rsi6_1h:.1f}")
        if rsi_str: print(f"   RSI(6): {' / '.join(rsi_str)}")

        if ctx.funding_rate is not None:
            print(f"   펀비: {ctx.funding_rate*100:.4f}% / {ctx.funding_interval_h}h")

        if rep is not None:
            ok, tot = rep.passed_count, rep.total_count
            status = "✅ ALL PASS" if rep.all_passed else f"⚠️  {ok}/{tot} PASS"
            print(f"   필터: {status}")
            if not rep.all_passed:
                for r in rep.failed:
                    print(f"      ✗ {r.name}: {r.detail}")


def save_json(results: list[tuple[Signal, flt.FilterReport | None]]) -> str:
    """JSON 파일로 저장."""
    os.makedirs(config.OUTPUT_DIR, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = os.path.join(config.OUTPUT_DIR, f"scan_{ts}.json")

    payload = {
        "scanned_at": datetime.now(timezone.utc).isoformat(),
        "config": {
            "exchange": config.EXCHANGE_ID,
            "bb_period": config.BB_PERIOD,
            "rsi_length": config.RSI_LENGTH,
            "monitoring_pctl": config.MONITORING_PCTL,
            "good_short_pctl": config.GOOD_SHORT_PCTL,
            "os_threshold": config.OS_DEVIATION,
            "mad_os_threshold": config.MAD_OS_DEVIATION,
        },
        "signals": [
            {
                **sig.to_dict(),
                "filter_report": (
                    {
                        "all_passed": rep.all_passed,
                        "passed_count": rep.passed_count,
                        "total_count": rep.total_count,
                        "results": [
                            {"name": r.name, "passed": r.passed, "detail": r.detail}
                            for r in rep.results
                        ],
                    } if rep else None
                ),
            }
            for sig, rep in results
            if sig.grade != "NONE"
        ],
    }

    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
    log.info(f"💾 JSON 저장: {path}")
    return path


# ============================================================
# 진입점
# ============================================================
def run(once: bool = True, output_json: bool = True, send_telegram: bool = True):
    """스캔 실행."""
    while True:
        results = scan_once()
        print_summary(results)
        if output_json:
            save_json(results)
        if send_telegram and config.TELEGRAM_ENABLED:
            actionable = filter_actionable(results, min_grade=config.TELEGRAM_MIN_GRADE)
            for sig, _ in actionable:
                tg.notify(sig)

        if once:
            break
        log.info(f"⏰ 다음 스캔까지 {config.SCAN_INTERVAL_SEC}초 대기...")
        time.sleep(config.SCAN_INTERVAL_SEC)
