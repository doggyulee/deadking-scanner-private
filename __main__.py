"""
데드킹 스캐너 CLI 진입점

사용법:
  python -m deadking_scanner                  # 1회 스캔
  python -m deadking_scanner --loop           # 15분마다 반복
  python -m deadking_scanner --min-grade S    # S 등급 이상만 출력
  python -m deadking_scanner --no-json        # JSON 저장 안 함
  python -m deadking_scanner --exchange bitget
"""
import argparse
import logging
import sys

import config
import scanner


def setup_logging(level: str = "INFO"):
    logging.basicConfig(
        level=getattr(logging, level),
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    # ccxt 디버그는 끔
    logging.getLogger("ccxt").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def main():
    parser = argparse.ArgumentParser(
        prog="deadking_scanner",
        description="데드킹 하이엔드 기법 기반 코인 숏 시그널 스캐너",
    )
    parser.add_argument(
        "--loop", action="store_true",
        help="설정된 주기 (기본 15분) 으로 반복 스캔",
    )
    parser.add_argument(
        "--exchange", default=None,
        choices=["binance", "bitget"],
        help=f"거래소 (기본: {config.EXCHANGE_ID})",
    )
    parser.add_argument(
        "--top", type=int, default=None,
        help=f"거래대금 상위 N개 (기본: {config.TOP_N_BY_VOLUME})",
    )
    parser.add_argument(
        "--min-grade", default=None,
        choices=["S+", "S", "A", "B", "C+", "C"],
        help="이 등급 이상만 출력/알림 (기본: A)",
    )
    parser.add_argument(
        "--no-json", action="store_true",
        help="JSON 결과 저장 안 함",
    )
    parser.add_argument(
        "--no-telegram", action="store_true",
        help="텔레그램 알림 비활성화",
    )
    parser.add_argument(
        "--log-level", default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )

    args = parser.parse_args()

    # 명령줄 인자 → config 오버라이드
    if args.exchange:
        config.EXCHANGE_ID = args.exchange
    if args.top:
        config.TOP_N_BY_VOLUME = args.top
    if args.min_grade:
        config.TELEGRAM_MIN_GRADE = args.min_grade
    if args.no_telegram:
        config.TELEGRAM_ENABLED = False
    if args.log_level:
        config.LOG_LEVEL = args.log_level

    setup_logging(config.LOG_LEVEL)

    try:
        scanner.run(
            once=not args.loop,
            output_json=not args.no_json,
            send_telegram=not args.no_telegram and config.TELEGRAM_ENABLED,
        )
    except KeyboardInterrupt:
        print("\n중단됨.")
        sys.exit(0)


if __name__ == "__main__":
    main()
