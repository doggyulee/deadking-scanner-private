"""GENIUS 절대값 가드 vs 운영자 SCAM? 일치 케이스 로깅 + 누적 통계.

가설:
  classify_pctl 의 `dev < 0` / `dev < MIN_ABS_BB_DEVIATION` 가드와
  classify_operator 의 `bb_dev >= OP_MONITORING_BB (7%)` 게이트가
  거짓 양성(역하이엔드 못 만한 박스권 펌프) 을 걸러주는가.

  운영자가 사후에 SCAM/SCAM? 태그를 단 종목이 그 가드에 걸린 사례를
  카운트해서 가드 적중률을 누적.

상태 파일: output/guard_agreement_stats.json
{
  "cases": [
    {
      "case_id": "GENIUS_2026-05-23",
      "symbol": "GENIUS/USDT:USDT",
      "blocked_at": "2026-05-23T14:15:00+00:00",
      "bb_dev_at_block": -0.004,
      "guard_triggered": "pctl: dev<0",
      "operator_subsequent_tag": "Mon SCAM?",
      "operator_tag_time": "2026-05-24T15:35:00+00:00",
      "agreement": true
    }
  ],
  "summary": {
    "total_blocked_cases": N,
    "operator_confirmed_scam": K,
    "hit_rate": K/N
  }
}
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import config
from classifier import SignalContext, classify_operator, classify_pctl

STATS_FILE = HERE / "output" / "guard_agreement_stats.json"


def simulate_genius_minus_04() -> dict:
    """어제 14:15 무렵 GENIUS BB-0.4% 가정 입력에 대한 가드 결과 검증."""
    # 13:41 스냅샷의 BB upper, 가격을 BB upper 대비 -0.4% 로 후퇴시킨 가정
    bb_upper = 0.6678393366610559
    price = bb_upper * (1 - 0.004)
    ctx = SignalContext(
        symbol="GENIUS/USDT:USDT", price=price,
        bb_upper_1d=bb_upper, bb_middle_1d=0.53195, bb_lower_1d=0.396,
        bb_dev_1d=-0.004,           # 핵심 입력 = BB 상단 대비 -0.4%
        bb_dev_pctl_1d=70.0,        # 가정: 백분위는 그래도 높을 수 있음
        rsi6_1d=78.0, rsi6_4h=70.0, rsi6_1h=60.0,
        ma5_deviation=0.20,         # 5MA 이격은 살아있다 가정
    )
    op = classify_operator(ctx)
    pctl = classify_pctl(ctx)
    return {
        "input": {"bb_dev_1d": -0.004, "ma5_deviation": 0.20, "bb_dev_pctl_1d": 70.0},
        "operator_grade": op.grade,
        "operator_reasons": op.reasons,
        "pctl_grade": pctl.grade,
        "pctl_reasons": pctl.reasons,
        "blocked": op.grade == "NONE" and pctl.grade == "NONE",
    }


def load_stats() -> dict:
    if not STATS_FILE.exists():
        return {"cases": [], "summary": {}}
    with open(STATS_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_stats(stats: dict) -> None:
    STATS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATS_FILE, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)


def add_case(stats: dict, case: dict) -> None:
    stats.setdefault("cases", []).append(case)
    cases = stats["cases"]
    blocked = [c for c in cases if c.get("blocked", True)]
    confirmed = [c for c in blocked if c.get("agreement")]
    stats["summary"] = {
        "total_blocked_cases": len(blocked),
        "operator_confirmed_scam": len(confirmed),
        "hit_rate": round(len(confirmed) / len(blocked), 3) if blocked else 0.0,
    }


def main() -> int:
    print("=" * 78)
    print("GENIUS 가드 시뮬레이션 (BB -0.4% 입력)")
    print("=" * 78)
    sim = simulate_genius_minus_04()
    print(f"  입력: BB {sim['input']['bb_dev_1d']*100:+.2f}%  "
          f"5MA {sim['input']['ma5_deviation']*100:+.2f}%  "
          f"pctl {sim['input']['bb_dev_pctl_1d']:.0f}%")
    print(f"  운영자 모드 → {sim['operator_grade']}")
    for r in sim["operator_reasons"]:
        print(f"    · {r}")
    print(f"  백분위 모드 → {sim['pctl_grade']}")
    for r in sim["pctl_reasons"]:
        print(f"    · {r}")
    print(f"  두 모드 모두 차단? {sim['blocked']}")

    # 통계 누적
    stats = load_stats()
    case = {
        "case_id": "GENIUS_2026-05-23_to_24",
        "symbol": "GENIUS/USDT:USDT",
        "blocked_at": "2026-05-23T14:15:00+00:00",  # 14:15 스캔에 GENIUS 없음 = 차단된 시점 추정
        "bb_dev_at_block_assumed": -0.004,
        "guard_triggered": ("pctl: dev<0  AND  operator: bb_dev<OP_MONITORING_BB(7%)"
                            if sim["blocked"] else "none"),
        "operator_subsequent_tag": "Mon SCAM?",
        "operator_tag_time": "2026-05-24T15:35:00+09:00",   # KST 추정
        "blocked": sim["blocked"],
        "agreement": sim["blocked"],   # 차단 + 운영자 의심 = 일치
        "logged_at": datetime.now(timezone.utc).isoformat(),
        "note": ("어제 13:41 스캔에서 BB +1.75% 로 S 등급 → 14:15/14:24 스캔에서 사라짐. "
                 "BB 가 음수로 후퇴해 dev<0 가드에 걸린 것으로 추정. "
                 "오늘 운영자가 SCAM? 태그 → 가드 차단이 사후적으로 정당화됨."),
    }
    add_case(stats, case)
    save_stats(stats)

    print()
    print("=" * 78)
    print("가드 일치 통계 (누적)")
    print("=" * 78)
    print(f"  전체 차단 케이스: {stats['summary']['total_blocked_cases']}건")
    print(f"  운영자 SCAM 확인: {stats['summary']['operator_confirmed_scam']}건")
    print(f"  적중률: {stats['summary']['hit_rate']*100:.1f}%")
    print(f"  → {STATS_FILE.relative_to(HERE)} 저장")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
