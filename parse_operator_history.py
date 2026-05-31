"""
운영자 채널 텔레그램 HTML 익스포트 → output/operator_history.jsonl 변환.

입력:
  C:\\Users\\tlsan\\Downloads\\Telegram Desktop\\ChatExport_2026-05-24\\messages.html
  C:\\Users\\tlsan\\Downloads\\Telegram Desktop\\ChatExport_2026-05-24\\messages2.html

HTML 구조:
  <div class="message">
    <div class="pull_right date details" title="11.04.2026 20:58:29 UTC+07:00">…</div>
    <div class="body">
      <div class="text">
        Grade<br/><strong>SYMUSDT</strong>, BB +X%, 5MA +Y% (TAG)<br/>
        <strong>SYM2USDT</strong>, BB +A%, 5MA +B%
      </div>
    </div>
  </div>

핵심 원칙:
  - 한 메시지에 여러 심볼이 있을 수 있다 → <strong> 태그 단위로 분리
  - get_text(' ', strip=True) 로 텍스트 추출 (개행 보존하면 strong 사이 텍스트가 깨짐)
  - 심볼은 한자/유니코드 가능 (币安人生USDT 등)

출력 (jsonl, 라인당 한 객체):
  {timestamp, grade, symbol, bb_pct, ma5_pct, tags, mcap, raw}
  시스템 메시지(🔴 SYSTEM DOWN / 🟢 SCANNER ON 등)는 별도 분류.

검증 기준 (사용자 사전 분석):
  ~1508 신호 / 264 심볼 / 35 시스템 이벤트 (±5%)
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

from bs4 import BeautifulSoup, NavigableString, Tag


HERE = Path(__file__).resolve().parent
DEFAULT_INPUTS = [
    Path(r"C:\Users\tlsan\Downloads\Telegram Desktop\ChatExport_2026-05-24\messages.html"),
    Path(r"C:\Users\tlsan\Downloads\Telegram Desktop\ChatExport_2026-05-24\messages2.html"),
]
OUTPUT_PATH = HERE / "output" / "operator_history.jsonl"


# ============================================================
# 패턴
# ============================================================
# 등급 — 길이 긴 패턴 우선 (regex alternation 은 left-to-right)
GRADE_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("Good short zone MAD OS",   re.compile(r"good\s*short\s*zone\s*mad\s*os\b", re.I)),
    ("Good short zone OS",       re.compile(r"good\s*short\s*zone\s*os\b", re.I)),
    ("Good short zone",          re.compile(r"good\s*short\s*zone\b", re.I)),
    ("Deadking zone OS",         re.compile(r"deadking\s*zone\s*os\b", re.I)),
    ("Deadking zone",            re.compile(r"deadking\s*zone\b", re.I)),
    ("Monitoring MAD OS",        re.compile(r"\bmon(?:itoring)?\s*mad\s*os\b", re.I)),
    ("Monitoring OS",            re.compile(r"\bmon(?:itoring)?\s*os\b", re.I)),
    ("Monitoring",               re.compile(r"\bmon(?:itoring)?\b", re.I)),
]

# 심볼에서 USDT 접미사 제거
SYMBOL_SUFFIX_RE = re.compile(r"^(.+?)\s*[:/]?\s*USDT$", re.I)

# strong 뒤따르는 텍스트에서 BB / 5MA / tag 추출
BB_RE = re.compile(r"BB\s*([+-]?\d+(?:\.\d+)?)\s*%", re.I)
MA5_RE = re.compile(r"5\s*MA\s*([+-]?\d+(?:\.\d+)?)\s*%", re.I)
MCAP_RE = re.compile(r"M[Cc]ap\s*\$?\s*(\d+(?:\.\d+)?)\s*([BMK])", re.I)
TAG_GAP_RE = re.compile(r"\(\s*GAP\s*\)|\bGAP\b", re.I)
TAG_SCAMQ_RE = re.compile(r"\(\s*SCAM\s*\?\s*\)|SCAM\s*\?", re.I)  # SCAM? 먼저
TAG_SCAM_RE = re.compile(r"\(\s*SCAM\s*\)|\bSCAM\b(?!\s*\?)", re.I)

# 시스템 메시지
SYSTEM_PATTERNS = {
    "SYSTEM_DOWN":   re.compile(r"SYSTEM\s*DOWN", re.I),
    "SCANNER_ON":    re.compile(r"SCANNER\s*ON", re.I),
    "SCANNER_OFF":   re.compile(r"SCANNER\s*OFF", re.I),
}

# 텔레그램 익스포트 timestamp 포맷: "11.04.2026 20:58:29 UTC+07:00"
TS_FMT = "%d.%m.%Y %H:%M:%S UTC%z"


# ============================================================
# 데이터 클래스
# ============================================================
@dataclass
class SignalRecord:
    timestamp: str
    grade: Optional[str]
    symbol: Optional[str]
    bb_pct: Optional[float]
    ma5_pct: Optional[float]
    tags: list[str] = field(default_factory=list)
    mcap: Optional[str] = None
    raw: str = ""

    def to_json(self) -> dict:
        d = self.__dict__.copy()
        return d


@dataclass
class SystemEvent:
    timestamp: str
    kind: str       # SYSTEM_DOWN / SCANNER_ON / ...
    raw: str

    def to_json(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "kind": "system",
            "event": self.kind,
            "raw": self.raw,
        }


# ============================================================
# 파싱 헬퍼
# ============================================================
def parse_timestamp(title: str) -> Optional[str]:
    """텔레그램 익스포트 datetime title → UTC ISO 8601."""
    if not title:
        return None
    try:
        dt = datetime.strptime(title.strip(), TS_FMT)
    except ValueError:
        return None
    return dt.astimezone(timezone.utc).isoformat()


def extract_grade(text: str) -> Optional[str]:
    for name, pat in GRADE_PATTERNS:
        if pat.search(text):
            return name
    return None


def normalize_symbol(raw: str) -> str:
    """'RAVEUSDT' 또는 'RAVE/USDT' → 'RAVE'."""
    s = raw.strip()
    m = SYMBOL_SUFFIX_RE.match(s)
    if m:
        return m.group(1).strip()
    return s


def trailing_text_for_strong(strong: Tag) -> str:
    """
    <strong>X</strong> 다음부터 다음 <strong> 직전까지의 모든 텍스트.
    한 메시지에 심볼이 여러 개일 때 각 심볼의 데이터를 분리하는 데 필요.
    """
    parts: list[str] = []
    for sib in strong.next_siblings:
        if isinstance(sib, Tag):
            if sib.name == "strong":
                break
            if sib.name == "br":
                continue
            parts.append(sib.get_text(" ", strip=False))
        elif isinstance(sib, NavigableString):
            parts.append(str(sib))
    return " ".join(p for p in parts if p).strip()


def extract_tags(text: str) -> tuple[list[str], Optional[str]]:
    """텍스트에서 GAP / SCAM / SCAM? / MCap 추출."""
    tags: list[str] = []
    if TAG_GAP_RE.search(text):
        tags.append("GAP")
    if TAG_SCAMQ_RE.search(text):
        tags.append("SCAM?")
    elif TAG_SCAM_RE.search(text):
        tags.append("SCAM")

    mcap = None
    m = MCAP_RE.search(text)
    if m:
        num = m.group(1)
        unit = m.group(2).upper()
        mcap = f"${num}{unit}"
        tags.append(f"MCap {mcap}")
    return tags, mcap


def detect_system(text: str) -> Optional[str]:
    for kind, pat in SYSTEM_PATTERNS.items():
        if pat.search(text):
            return kind
    return None


# ============================================================
# 메시지 → 레코드 목록
# ============================================================
def parse_message(text_div: Tag, ts_iso: str) -> list:
    """한 div.message → SignalRecord 또는 SystemEvent 리스트 (심볼별로 펼침)."""
    full_text = text_div.get_text(" ", strip=True)
    if not full_text:
        return []

    sys_kind = detect_system(full_text)
    if sys_kind:
        return [SystemEvent(timestamp=ts_iso, kind=sys_kind, raw=full_text)]

    grade = extract_grade(full_text)
    strongs = text_div.find_all("strong")

    # 등급도 없고 strong 도 없으면 — 그냥 텍스트 메시지 (운영자 잡담 등)
    if grade is None and not strongs:
        return []

    # strong 없이 등급만 있는 (드물지만) — 한 레코드만 (심볼 미상)
    if not strongs:
        return [
            SignalRecord(
                timestamp=ts_iso, grade=grade, symbol=None,
                bb_pct=None, ma5_pct=None, tags=[], mcap=None,
                raw=full_text,
            )
        ]

    records: list[SignalRecord] = []
    for strong in strongs:
        sym_raw = strong.get_text(strip=True)
        if not sym_raw:
            continue
        symbol = normalize_symbol(sym_raw)
        trail = trailing_text_for_strong(strong)

        bb = ma5 = None
        bm = BB_RE.search(trail)
        if bm:
            try:
                bb = float(bm.group(1))
            except ValueError:
                pass
        mm = MA5_RE.search(trail)
        if mm:
            try:
                ma5 = float(mm.group(1))
            except ValueError:
                pass

        tags, mcap = extract_tags(trail)

        records.append(
            SignalRecord(
                timestamp=ts_iso, grade=grade, symbol=symbol,
                bb_pct=bb, ma5_pct=ma5, tags=tags, mcap=mcap,
                raw=full_text,
            )
        )
    return records


# ============================================================
# 메인
# ============================================================
def parse_file(path: Path) -> tuple[list[SignalRecord], list[SystemEvent], int]:
    """단일 HTML 파일 파싱. (signals, system_events, total_messages)."""
    if not path.exists():
        print(f"❌ 입력 없음: {path}", file=sys.stderr)
        return [], [], 0
    with open(path, encoding="utf-8") as f:
        soup = BeautifulSoup(f.read(), "lxml")
    msgs = soup.select("div.message")
    signals: list[SignalRecord] = []
    systems: list[SystemEvent] = []
    for m in msgs:
        text_div = m.select_one("div.text")
        if not text_div:
            continue
        dt_div = m.select_one("div.pull_right.date.details")
        ts_iso = parse_timestamp(dt_div.get("title", "") if dt_div else "")
        if not ts_iso:
            continue
        parts = parse_message(text_div, ts_iso)
        for p in parts:
            if isinstance(p, SystemEvent):
                systems.append(p)
            else:
                signals.append(p)
    return signals, systems, len(msgs)


def print_stats(signals: list[SignalRecord], systems: list[SystemEvent]) -> None:
    print()
    print("=" * 78)
    print(f"파싱 결과 요약")
    print("=" * 78)
    print(f"  총 시그널 레코드      : {len(signals)}")
    syms = [s.symbol for s in signals if s.symbol]
    print(f"  고유 심볼             : {len(set(syms))}")
    print(f"  시스템 이벤트         : {len(systems)}")

    # 등급별 카운트
    grade_count = Counter(s.grade for s in signals if s.grade)
    print()
    print("─ 등급별 분포 ─")
    for g, c in grade_count.most_common():
        print(f"  {g:<24} {c}")

    # 심볼 빈도 TOP 20
    print()
    print("─ 심볼 빈도 TOP 20 ─")
    sym_count = Counter(syms)
    for s, c in sym_count.most_common(20):
        print(f"  {s:<20} {c}")

    # 태그 분포
    print()
    print("─ 태그 분포 ─")
    tag_count = Counter()
    for s in signals:
        for t in s.tags:
            # MCap 은 종별로 다 다르니 prefix 만 카운트
            key = "MCap" if t.startswith("MCap") else t
            tag_count[key] += 1
    for t, c in tag_count.most_common():
        print(f"  {t:<10} {c}")

    # 등급별 BB% / 5MA% 분포 (p10/p50/p90)
    print()
    print("─ 등급별 BB% / 5MA% 분포 ─")
    print(f"  {'등급':<24} {'n':<5} {'BB p10':>8} {'BB p50':>8} {'BB p90':>8} "
          f"{'5MA p10':>9} {'5MA p50':>9} {'5MA p90':>9}")
    for g in sorted(grade_count, key=lambda g: -grade_count[g]):
        bs = sorted(s.bb_pct for s in signals if s.grade == g and s.bb_pct is not None)
        ms = sorted(s.ma5_pct for s in signals if s.grade == g and s.ma5_pct is not None)

        def pct(arr, p):
            if not arr: return None
            k = max(0, min(len(arr) - 1, int(round((p / 100) * (len(arr) - 1)))))
            return arr[k]

        def fmt(v):
            return f"{v:+.1f}" if v is not None else "  -  "

        print(f"  {g:<24} {len(bs):<5} {fmt(pct(bs,10)):>8} {fmt(pct(bs,50)):>8} {fmt(pct(bs,90)):>8} "
              f"{fmt(pct(ms,10)):>9} {fmt(pct(ms,50)):>9} {fmt(pct(ms,90)):>9}")

    # 시스템 이벤트 분포
    print()
    print("─ 시스템 이벤트 ─")
    sys_count = Counter(s.kind for s in systems)
    for k, c in sys_count.most_common():
        print(f"  {k:<16} {c}")


def main(argv: list[str]) -> int:
    inputs = [Path(a) for a in argv[1:]] if len(argv) > 1 else DEFAULT_INPUTS

    all_signals: list[SignalRecord] = []
    all_systems: list[SystemEvent] = []
    grand_total = 0
    for p in inputs:
        sigs, syss, total = parse_file(p)
        print(f"📄 {p.name}: {total} 메시지, {len(sigs)} 시그널, {len(syss)} 시스템")
        all_signals.extend(sigs)
        all_systems.extend(syss)
        grand_total += total

    # 출력 (시간순 정렬)
    all_signals.sort(key=lambda r: r.timestamp)
    all_systems.sort(key=lambda r: r.timestamp)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        for r in all_signals:
            f.write(json.dumps(r.to_json(), ensure_ascii=False) + "\n")
        for r in all_systems:
            f.write(json.dumps(r.to_json(), ensure_ascii=False) + "\n")
    print(f"💾 저장: {OUTPUT_PATH}  ({len(all_signals)} 시그널 + {len(all_systems)} 시스템)")

    print_stats(all_signals, all_systems)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
