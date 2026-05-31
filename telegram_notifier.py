"""
텔레그램 알림 (선택)
- 일정 등급 이상 시그널만 발송
- requests 사용 (의존성 최소화)
"""
from __future__ import annotations

import logging
from typing import Optional

import urllib.request
import urllib.parse
import json

import config
from classifier import Signal, GRADE_EMOJI, grade_at_least

log = logging.getLogger(__name__)


def format_signal(signal: Signal) -> str:
    """텔레그램 메시지 포맷."""
    ctx = signal.context
    emoji = GRADE_EMOJI.get(signal.grade, "")

    lines = [
        f"{emoji} <b>{signal.name}</b>",
        f"<code>{signal.symbol}</code>",
        f"가격: <code>{ctx.price:.6g}</code>",
        f"권장 비중: <code>{signal.weight*100:.2f}%</code>",
        "",
        f"📊 BB(22) 상단: <code>{ctx.bb_upper_1d:.6g}</code>",
        f"   이격: <code>{ctx.bb_dev_1d*100:+.2f}%</code> "
        f"({ctx.bb_dev_pctl_1d:.0f}퍼센타일)",
    ]

    if ctx.rsi6_4h is not None or ctx.rsi6_1h is not None:
        rsi_parts = []
        if ctx.rsi6_1d is not None:
            rsi_parts.append(f"1D={ctx.rsi6_1d:.1f}")
        if ctx.rsi6_4h is not None:
            rsi_parts.append(f"4H={ctx.rsi6_4h:.1f}")
        if ctx.rsi6_1h is not None:
            rsi_parts.append(f"1H={ctx.rsi6_1h:.1f}")
        lines.append(f"📈 RSI(6): {' / '.join(rsi_parts)}")

    if ctx.funding_rate is not None:
        lines.append(
            f"💸 펀비: <code>{ctx.funding_rate*100:.4f}%</code> "
            f"({ctx.funding_interval_h}h 주기)"
        )

    lines.append("")
    lines.append("<b>분류 사유:</b>")
    for r in signal.reasons:
        lines.append(f"• {r}")

    # 차트 / 청산맵 링크
    base = signal.symbol.split("/")[0].replace(":USDT", "")
    lines.append("")
    lines.append(
        f'<a href="https://www.tradingview.com/chart/?symbol=BINANCE:{base}USDT.P">TradingView</a> '
        f'· <a href="https://www.coinglass.com/pro/futures/LiquidationHeatMapNew/{base}">청산맵</a>'
    )

    return "\n".join(lines)


def send_telegram(text: str, parse_mode: str = "HTML") -> bool:
    """텔레그램 봇 메시지 전송."""
    if not config.TELEGRAM_ENABLED:
        return False
    token = config.TELEGRAM_BOT_TOKEN
    chat_id = config.TELEGRAM_CHAT_ID
    if not token or not chat_id:
        log.warning("텔레그램 토큰/채팅ID 미설정")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": "true",
    }).encode()

    try:
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode())
            if not result.get("ok"):
                log.error(f"텔레그램 전송 실패: {result}")
                return False
            return True
    except Exception as e:
        log.error(f"텔레그램 요청 실패: {e}")
        return False


def notify(signal: Signal) -> None:
    """시그널 등급이 임계값 이상이면 텔레그램 알림."""
    if not config.TELEGRAM_ENABLED:
        return
    if not grade_at_least(signal.grade, config.TELEGRAM_MIN_GRADE):
        return
    text = format_signal(signal)
    ok = send_telegram(text)
    if ok:
        log.info(f"📱 텔레그램 알림 전송: {signal.symbol} [{signal.grade}]")
