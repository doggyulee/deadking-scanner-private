"""
강의(리딩) 채널 텔레그램 HTML 익스포트 → output/operator_trades.jsonl 변환.

입력 (기본):
  C:\\Users\\tlsan\\Downloads\\Telegram Desktop\\ChatExport_2026-05-24 (1)\\messages.html
  (폴더명에 공백 + 괄호 포함 — raw string 또는 Path() 사용 필수)

분류 (kind):
  - ACTION         : "## 현시점 / ## 추가로 현시점 ... 숏/롱 포지션은? 비중 X%"
                     {symbol, direction, weight_pct, is_additional, raw, time}
  - ACTION_OUT     : "## ... 숏/롱 포지션은? 전량" (보조 — 100% 종료 시각)
                     {symbol, direction, weight_pct=100, is_full_out=True, ...}
  - BRIEFING       : "<무언가> 브리핑 📌" (시장/주말/일일 브리핑)
  - MONITORING_SHARE: "모니터링 종목 공유" / "관심 모니터링" 키워드
  - ANNOUNCE       : "강의" / "OT" / "youtube" / "라이브" 류 공지

출력 jsonl (시간순):
  {timestamp, kind, ...필드, raw}

검증값 (사용자 사전 분석):
  - ACTION 정확히 15건
  - 종목 10개 정확히: STO, NOM, JOE, KOMA, RAVE, SPK, LAB, SAGA, UB, BEAT
  - 모두 숏 방향
  - 비중 분포: 50% × 8, 75% × 6, 90% × 1
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from bs4 import BeautifulSoup, Tag


HERE = Path(__file__).resolve().parent
DEFAULT_INPUT = Path(r"C:\Users\tlsan\Downloads\Telegram Desktop\ChatExport_2026-05-24 (1)\messages.html")
OUTPUT_PATH = HERE / "output" / "operator_trades.jsonl"

TS_FMT = "%d.%m.%Y %H:%M:%S UTC%z"


# ============================================================
# 정규식
# ============================================================
# 사용자 사양 그대로. [A-Z0-9一-鿿] = ASCII 종목 + 한자 종목명 허용.
ACTION_RE = re.compile(
    r"##\s*(현시점|추가로 현시점)\s+"
    r"([A-Z0-9一-鿿]+)\s+"
    r"(숏|롱)\s+포지션은?\s+"
    r"비중\s*(\d+)\s*%"
)

# 전량 정리 — 부수 정보로 같이 잡되 ACTION 카운트엔 포함 X
ACTION_OUT_RE = re.compile(
    r"##\s*(현시점|추가로 현시점)\s+"
    r"([A-Z0-9一-鿿]+)\s+"
    r"(숏|롱)\s+포지션은?\s+전량"
)

BRIEFING_RE = re.compile(r"(\S+)\s*브리핑\s*📌")

MONITORING_SHARE_KEYWORDS = ("모니터링 종목 공유", "관심 모니터링")
ANNOUNCE_KEYWORDS = ("강의", "OT", "youtube", "YouTube", "라이브")


# ============================================================
# 데이터 클래스
# ============================================================
@dataclass
class Record:
    timestamp: str
    kind: str
    raw: str
    fields: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        d = {"timestamp": self.timestamp, "kind": self.kind, **self.fields, "raw": self.raw}
        return d


# ============================================================
# 헬퍼
# ============================================================
def parse_ts(title: str) -> Optional[str]:
    if not title:
        return None
    try:
        dt = datetime.strptime(title.strip(), TS_FMT)
    except ValueError:
        return None
    return dt.astimezone(timezone.utc).isoformat()


def classify(text: str) -> Optional[tuple[str, dict]]:
    """텍스트를 보고 (kind, fields) 반환. 매칭 안 되면 None."""
    # ACTION (비중 X%)
    m = ACTION_RE.search(text)
    if m:
        prefix, sym, direction, pct = m.groups()
        return "ACTION", {
            "symbol": sym,
            "direction": direction,
            "weight_pct": int(pct),
            "is_additional": prefix.startswith("추가로"),
        }

    # ACTION_OUT (전량)
    m = ACTION_OUT_RE.search(text)
    if m:
        prefix, sym, direction = m.groups()
        return "ACTION_OUT", {
            "symbol": sym,
            "direction": direction,
            "weight_pct": 100,
            "is_additional": prefix.startswith("추가로"),
            "is_full_out": True,
        }

    # BRIEFING (별도 ## 액션이 없는 메시지일 때만)
    if "##" not in text:
        m = BRIEFING_RE.search(text)
        if m:
            subject = m.group(1)
            return "BRIEFING", {"subject": subject}

        # MONITORING_SHARE
        if any(kw in text for kw in MONITORING_SHARE_KEYWORDS):
            return "MONITORING_SHARE", {}

        # ANNOUNCE
        if any(kw in text for kw in ANNOUNCE_KEYWORDS):
            return "ANNOUNCE", {}

    return None


# ============================================================
# 파싱
# ============================================================
def parse(html_path: Path) -> list[Record]:
    if not html_path.exists():
        print(f"❌ 입력 없음: {html_path}", file=sys.stderr)
        return []
    with open(html_path, encoding="utf-8") as f:
        soup = BeautifulSoup(f.read(), "lxml")
    msgs = soup.select("div.message")
    records: list[Record] = []
    for m in msgs:
        td = m.select_one("div.text")
        if not td:
            continue
        text = td.get_text(" ", strip=True)
        if not text:
            continue
        dt_div = m.select_one("div.pull_right.date.details")
        ts = parse_ts(dt_div.get("title", "") if dt_div else "")
        if not ts:
            continue
        cls = classify(text)
        if cls is None:
            continue
        kind, fields = cls
        records.append(Record(timestamp=ts, kind=kind, raw=text, fields=fields))
    records.sort(key=lambda r: r.timestamp)
    return records


def print_stats(records: list[Record]) -> None:
    kinds = Counter(r.kind for r in records)
    print()
    print("=" * 78)
    print("강의 채널 파싱 결과")
    print("=" * 78)
    print(f"  총 분류 레코드: {len(records)}")
    for k, c in kinds.most_common():
        print(f"  {k:<20} {c}")

    actions = [r for r in records if r.kind == "ACTION"]
    if actions:
        print()
        print(f"─ ACTION 상세 ({len(actions)} 건, 검증값: 15) ─")
        for r in actions:
            f = r.fields
            additional = " +추가" if f.get("is_additional") else "  "
            print(f"  {r.timestamp[:19].replace('T',' ')}  "
                  f"{f['symbol']:<6} {f['direction']:<2} {f['weight_pct']:>3}%{additional}")

        syms = sorted({r.fields["symbol"] for r in actions})
        print()
        print(f"─ 종목 (총 {len(syms)} 개) ─")
        print(f"  {', '.join(syms)}")

        pcts = Counter(r.fields["weight_pct"] for r in actions)
        print()
        print(f"─ 비중 분포 ─")
        for p in sorted(pcts, reverse=True):
            print(f"  {p:>3}% : {pcts[p]}회")

        dirs = Counter(r.fields["direction"] for r in actions)
        print(f"─ 방향 ─ {dict(dirs)}")

    full_outs = [r for r in records if r.kind == "ACTION_OUT"]
    if full_outs:
        print()
        print(f"─ ACTION_OUT (전량 정리, 보조 정보) — {len(full_outs)} 건 ─")
        for r in full_outs[:15]:
            f = r.fields
            print(f"  {r.timestamp[:19].replace('T',' ')}  {f['symbol']:<6} {f['direction']} 전량")
        if len(full_outs) > 15:
            print(f"  … 외 {len(full_outs)-15} 건")


def main(argv: list[str]) -> int:
    in_path = Path(argv[1]) if len(argv) > 1 else DEFAULT_INPUT
    records = parse(in_path)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r.to_json(), ensure_ascii=False) + "\n")
    print(f"💾 저장: {OUTPUT_PATH}  ({len(records)} 레코드)")
    print_stats(records)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
