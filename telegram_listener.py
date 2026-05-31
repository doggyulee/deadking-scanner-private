"""
운영자 텔레그램 채널 실시간 리스너 (Phase 1).

데드킹 하이엔드 검색기 채널(개인 채널, username 없음)에 올라오는
신호 메시지를 실시간으로 받아 파싱 → output/operator_signals.jsonl 에 누적 저장.

자격증명은 절대 코드에 박지 않고 .env 에서만 읽는다.
세션 파일(*.session)은 Telethon 이 생성하며 로그인 토큰이 들어있으므로
.gitignore 로 반드시 제외한다.

사용법:
    pip install -r requirements.txt
    # .env 에 TELEGRAM_API_ID / TELEGRAM_API_HASH 채운 뒤
    python -X utf8 telegram_listener.py

첫 실행:
  - my.telegram.org 인증으로 받은 API_ID/HASH 로 로그인 (2FA 코드 입력)
  - 채널 ID 미설정 상태면 dialog 들 순회하며 이름으로 매칭, 발견 시 .env 에 자동 저장
  - 못 찾으면 구독 중인 채널 이름 리스트를 출력 → 사용자가 .env 의 NAME 수정

이후 실행:
  - 저장된 채널 ID 로 바로 이벤트 핸들러 등록
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

try:
    from dotenv import load_dotenv, set_key
except ImportError:
    print("python-dotenv 가 설치돼 있지 않다. `pip install -r requirements.txt`", file=sys.stderr)
    raise

try:
    from telethon import TelegramClient, events
    from telethon.tl.types import Channel, Chat
except ImportError:
    print("telethon 이 설치돼 있지 않다. `pip install -r requirements.txt`", file=sys.stderr)
    raise


HERE = Path(__file__).resolve().parent
ENV_PATH = HERE / ".env"
SESSION_PATH = HERE / "operator_listener"   # → operator_listener.session
OUTPUT_DIR = HERE / "output"
OUTPUT_JSONL = OUTPUT_DIR / "operator_signals.jsonl"


# ============================================================
# .env 로딩
# ============================================================
def load_env() -> dict:
    if not ENV_PATH.exists():
        print(f".env 파일이 없다: {ENV_PATH}", file=sys.stderr)
        print(".env.example 을 복사해 만들고 값을 채운 뒤 다시 실행.", file=sys.stderr)
        sys.exit(2)
    load_dotenv(ENV_PATH)
    cfg = {
        "api_id": os.getenv("TELEGRAM_API_ID", "").strip(),
        "api_hash": os.getenv("TELEGRAM_API_HASH", "").strip(),
        "channel_name": os.getenv("TELEGRAM_OPERATOR_CHANNEL_NAME", "").strip(),
        "channel_id": os.getenv("TELEGRAM_OPERATOR_CHANNEL_ID", "").strip(),
    }
    missing = [k for k in ("api_id", "api_hash", "channel_name") if not cfg[k]]
    if missing:
        print(f".env 에 다음 값이 비어있다: {', '.join(missing)}", file=sys.stderr)
        sys.exit(2)
    try:
        cfg["api_id_int"] = int(cfg["api_id"])
    except ValueError:
        print(f"TELEGRAM_API_ID 가 정수가 아니다: {cfg['api_id']!r}", file=sys.stderr)
        sys.exit(2)
    cfg["channel_id_int"] = int(cfg["channel_id"]) if cfg["channel_id"] else None
    return cfg


# ============================================================
# 메시지 파서
# ============================================================
# 등급 — 길이가 긴 패턴부터 먼저 매칭 (regex alternation 은 left-to-right)
GRADE_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("Deadking zone OS",         re.compile(r"deadking\s*zone\s*os\b", re.I)),
    ("Deadking zone",            re.compile(r"deadking\s*zone\b", re.I)),
    ("Good short zone MAD OS",   re.compile(r"good\s*short\s*zone\s*mad\s*os\b", re.I)),
    ("Good short zone OS",       re.compile(r"good\s*short\s*zone\s*os\b", re.I)),
    ("Good short zone",          re.compile(r"good\s*short\s*zone\b", re.I)),
    # 약어 (운영자 채팅 표기)
    ("Good short zone MAD OS",   re.compile(r"\bgoodshort\s*mad\s*os\b", re.I)),
    ("Good short zone OS",       re.compile(r"\bgoodshort\s*os\b", re.I)),
    ("Good short zone",          re.compile(r"\bgoodshort\b", re.I)),
    ("Monitoring MAD OS",        re.compile(r"\bmon(?:itoring)?\s*mad\s*os\b", re.I)),
    ("Monitoring OS",            re.compile(r"\bmon(?:itoring)?\s*os\b", re.I)),
    ("Monitoring",               re.compile(r"\bmon(?:itoring)?\b", re.I)),
]

SYMBOL_RE = re.compile(r"\b([A-Z0-9]{2,12})\s*/\s*USDT\b", re.I)
# fallback: 단독 티커 (예: "AGT", "BAN") — / 없을 때
SYMBOL_FALLBACK_RE = re.compile(r"\b([A-Z]{2,10})\b")

BB_RE = re.compile(r"bb\s*[%]?\s*([+-]?\d+(?:\.\d+)?)\s*%?", re.I)
MA5_RE = re.compile(r"5\s*ma\s*[%]?\s*([+-]?\d+(?:\.\d+)?)\s*%?", re.I)
MCAP_RE = re.compile(r"mcap\s*\$?\s*([\d.]+)\s*([kmb])?", re.I)

TAG_PATTERNS = {
    "GAP": re.compile(r"\bgap\b", re.I),
    "SCAM?": re.compile(r"\bscam\s*\?", re.I),    # SCAM? 먼저 (? 가 있는 것)
    "SCAM": re.compile(r"\bscam\b(?!\s*\?)", re.I),
}


@dataclass
class ParsedSignal:
    timestamp: str            # ISO 8601 UTC
    grade: Optional[str]
    symbol: Optional[str]
    bb_pct: Optional[float]
    ma5_pct: Optional[float]
    tags: list[str]
    raw_text: str
    message_id: int
    channel_id: int


def parse_message(text: str) -> dict:
    """운영자 메시지 텍스트를 파싱해 구조화된 dict 반환. 실패해도 raw 는 유지."""
    if not text:
        return {"grade": None, "symbol": None, "bb_pct": None, "ma5_pct": None, "tags": []}

    grade = None
    for name, pat in GRADE_PATTERNS:
        if pat.search(text):
            grade = name
            break

    symbol = None
    m = SYMBOL_RE.search(text)
    if m:
        symbol = m.group(1).upper() + "/USDT"
    else:
        # 짧은 티커 단독 표기 fallback — 흔한 영단어/약어 제외
        EXCLUDE = {
            "BB", "MA", "OS", "MAD", "GAP", "SCAM", "USDT", "MCAP",
            "MON", "GOODSHORT", "DEADKING", "ZONE", "THE", "AND", "FOR",
        }
        for w in SYMBOL_FALLBACK_RE.findall(text):
            wu = w.upper()
            if wu in EXCLUDE:
                continue
            symbol = wu  # 첫 후보 채택
            break

    bb_pct = None
    bm = BB_RE.search(text)
    if bm:
        try:
            bb_pct = float(bm.group(1))
        except ValueError:
            pass

    ma5_pct = None
    mm = MA5_RE.search(text)
    if mm:
        try:
            ma5_pct = float(mm.group(1))
        except ValueError:
            pass

    tags: list[str] = []
    for tag, pat in TAG_PATTERNS.items():
        if pat.search(text):
            # SCAM? 매칭됐으면 SCAM 도 매칭되므로 중복 제거
            if tag == "SCAM" and "SCAM?" in tags:
                continue
            tags.append(tag)

    mc = MCAP_RE.search(text)
    if mc:
        num = mc.group(1)
        unit = (mc.group(2) or "").upper()
        tags.append(f"MCap ${num}{unit}" if unit else f"MCap ${num}")

    return {
        "grade": grade,
        "symbol": symbol,
        "bb_pct": bb_pct,
        "ma5_pct": ma5_pct,
        "tags": tags,
    }


# ============================================================
# 저장
# ============================================================
def append_jsonl(sig: ParsedSignal) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_JSONL, "a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(sig), ensure_ascii=False) + "\n")


# ============================================================
# 채널 탐색 (private 채널, username 없음 → 이름으로 매칭)
# ============================================================
async def find_channel_by_name(client: TelegramClient, target_name: str) -> Optional[int]:
    print(f"📡 dialog 들 순회 중... 대상 이름: {target_name!r}")
    exact: list[tuple[str, int]] = []
    fuzzy: list[tuple[str, int]] = []
    all_titles: list[str] = []
    async for dialog in client.iter_dialogs():
        entity = dialog.entity
        if not isinstance(entity, (Channel, Chat)):
            continue
        title = (dialog.name or getattr(entity, "title", "") or "").strip()
        if not title:
            continue
        all_titles.append(title)
        if title == target_name:
            exact.append((title, entity.id))
        elif target_name in title:
            fuzzy.append((title, entity.id))

    if len(exact) == 1:
        title, cid = exact[0]
        print(f"✅ 정확 일치: {title!r} → ID {cid}")
        return cid
    if len(exact) > 1:
        print("⚠️ 같은 이름 채널이 여러 개. 수동 확인 필요:")
        for t, cid in exact:
            print(f"   - {t} (ID={cid})")
        return None
    if len(fuzzy) == 1:
        title, cid = fuzzy[0]
        print(f"✅ 부분 일치: {title!r} → ID {cid}")
        return cid
    if len(fuzzy) > 1:
        print("⚠️ 부분 일치 채널 여러 개. 더 정확한 이름 필요:")
        for t, cid in fuzzy:
            print(f"   - {t} (ID={cid})")
        return None

    print("❌ 매칭 채널 없음.")
    print("─" * 60)
    print("구독 중인 채널/그룹 이름 목록:")
    for t in sorted(set(all_titles)):
        print(f"   • {t}")
    print("─" * 60)
    print("→ .env 의 TELEGRAM_OPERATOR_CHANNEL_NAME 을 위 목록에서 정확히 복사해 수정.")
    return None


def save_channel_id_to_env(channel_id: int) -> None:
    """발견된 채널 ID 를 .env 에 영구 저장."""
    set_key(str(ENV_PATH), "TELEGRAM_OPERATOR_CHANNEL_ID", str(channel_id), quote_mode="never")
    print(f"💾 .env 에 TELEGRAM_OPERATOR_CHANNEL_ID={channel_id} 저장 완료.")


# ============================================================
# 메인 루프
# ============================================================
async def run(cfg: dict) -> None:
    client = TelegramClient(str(SESSION_PATH), cfg["api_id_int"], cfg["api_hash"])
    await client.start()
    me = await client.get_me()
    print(f"🔐 로그인 완료: {me.first_name or ''} (@{me.username or '-'}, id={me.id})")

    channel_id = cfg["channel_id_int"]
    if channel_id is None:
        channel_id = await find_channel_by_name(client, cfg["channel_name"])
        if channel_id is None:
            await client.disconnect()
            sys.exit(3)
        save_channel_id_to_env(channel_id)

    # 채널 entity 확보 (peer 캐싱)
    try:
        target = await client.get_entity(channel_id)
        title = getattr(target, "title", str(channel_id))
        print(f"🎯 리스닝 대상: {title!r} (ID={channel_id})")
    except Exception as e:
        print(f"⚠️ 채널 entity 조회 실패: {e}")
        print(f"   ID={channel_id} 가 정확한지, 또는 이 계정이 해당 채널에 가입돼 있는지 확인.")
        await client.disconnect()
        sys.exit(4)

    print(f"💾 저장 경로: {OUTPUT_JSONL}")
    print("📨 메시지 대기 중 (Ctrl+C 로 종료)...")
    print()

    @client.on(events.NewMessage(chats=channel_id))
    async def handler(event):
        text = event.message.message or ""
        parsed = parse_message(text)
        sig = ParsedSignal(
            timestamp=datetime.now(timezone.utc).isoformat(),
            grade=parsed["grade"],
            symbol=parsed["symbol"],
            bb_pct=parsed["bb_pct"],
            ma5_pct=parsed["ma5_pct"],
            tags=parsed["tags"],
            raw_text=text,
            message_id=event.message.id,
            channel_id=channel_id,
        )
        append_jsonl(sig)
        preview = text.replace("\n", " ⏎ ")[:80]
        print(
            f"[{sig.timestamp[11:19]}] "
            f"{sig.grade or '?':<24} {sig.symbol or '?':<12} "
            f"BB={sig.bb_pct}% 5MA={sig.ma5_pct}% tags={sig.tags} | {preview}"
        )

    await client.run_until_disconnected()


def main() -> None:
    cfg = load_env()
    try:
        asyncio.run(run(cfg))
    except KeyboardInterrupt:
        print("\n🛑 종료.")


if __name__ == "__main__":
    main()
