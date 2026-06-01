"""
데드킹 하이엔드 스캐너 — 설정값
모든 임계값은 마스터 룰북 (1주차+2주차+3주차) 기준
"""

# ============================================================
# 거래소
# ============================================================
EXCHANGE_ID = "binance"          # binance | bitget
MARKET_TYPE = "swap"             # USDT 무기한 선물
QUOTE = "USDT"

# ============================================================
# 지표 파라미터 (룰북 §2 확정)
# ============================================================
BB_PERIOD = 22                   # 볼린저밴드 기간 (운영자 확정)
BB_STD = 2.0                     # 볼린저밴드 표준편차
RSI_LENGTH = 6                   # RSI 길이 (length=6, 단타용)
MA_FAST = 5                      # 5일 이평선

# ============================================================
# 시간프레임
# ============================================================
TF_DAILY = "1d"                  # 메인 DC: 일봉 BB 이격
TF_4H = "4h"                     # RSI 보조
TF_1H = "1h"                     # RSI 보조 + 펀비
TF_15M = "15m"                   # 사이드 DC: 15분봉 이격

# ============================================================
# 분류 모드 (운영자 절대 임계값 vs 백분위)
# ============================================================
# True  : 운영자 5/22-23 로그 기반 절대 임계값 (5MA 1순위 게이트, BB 2순위)
# False : 종목별 30일 분포 백분위 (스캐너 알파)
# 스캐너는 항상 두 모드 모두 분류해서 비교 출력하지만,
# "메인 등급"으로 어느 쪽을 쓸지 결정.
USE_OPERATOR_THRESHOLDS = True

# 운영자 절대 임계값 (5MA 우선)
# 2026-05-24: 운영자 채널 40일치 1508개 신호 통계 분석 결과(p10 기준)로 보정.
OP_MONITORING_BB = 0.07          # BB 상단 대비 +7%
OP_MONITORING_5MA = 0.14         # 5일선 대비 +14% (AND, 5MA가 1순위 게이트)
OP_GOOD_SHORT_BB = 0.13          # BB 상단 대비 +13% (실제 운영자 p10)
OP_GOOD_SHORT_5MA = 0.30         # 5일선 대비 +30% (실측 p10 일치)
OP_MAD_OS_BB = 0.35              # BB 상단 대비 +35% (실측 +36%)
OP_MAD_OS_5MA = 0.70             # 5일선 대비 +70% (실측 +70%)

# 시간 추적 기반 OS / MAD OS (운영자 모드 보조)
# 첫 신호 발생 가격 대비 후속 상승률
HISTORY_OS_PCT = 0.01            # +1% 추가 상승 → OS suffix
HISTORY_MAD_OS_PCT = 0.03        # +3% 추가 상승 → MAD OS suffix
HISTORY_EXPIRE_HOURS = 6         # 6시간 이상 미관측 시 history 만료
HISTORY_FILE = "./output/signal_history.json"

# ============================================================
# 신호 등급 임계값 — 백분위 모드 (룰북 §3 + 스캐너 알파)
# ============================================================
# BB 이격도 백분위 (종목별 30일 분포 기준)
# - Monitoring: 숏존 진입 (이격이 평소보다 큰 자리)
# - Good Short Zone: 이격이 충분 (95퍼센타일 이상)
MONITORING_PCTL = 70             # 70퍼센타일 이상 → Monitoring 등급
GOOD_SHORT_PCTL = 90             # 90퍼센타일 이상 → Good Short Zone

# OS / MAD OS 분류 (BB 상단 대비 가격이 얼마나 위로 튀었는가)
# deviation = (price - bb_upper) / bb_upper
OS_DEVIATION = 0.005             # 0.5% 이상 → OS (오버슈팅)
MAD_OS_DEVIATION = 0.025         # 2.5% 이상 → MAD OS (미친 오버슈팅)

# RSI 극단치 (룰북 §3, §8-3 RSI 99 단타)
RSI_HIGH = 95                    # 95+ = 참고
RSI_EXTREME = 99                 # 99+ = MAD 격상 트리거

# 다중 TF RSI 99가 동시에 뜨면 자동으로 MAD 등급 부스트
MAD_BOOST_RSI_TFS = ["4h", "1h"]  # 두 TF 모두 RSI(6) >= 99 → MAD 격상

# ============================================================
# 7가지 PASS 필터 (룰북 §4-1, §7-1)
# ============================================================
ATH_BUFFER = 0.95                # 현재가 < ATH * 0.95 (신고가 X)
LOOKBACK_HIGH_DAYS = 30          # 직전 30일 고점 미돌파
WICK_RATIO_MAX = 0.30            # 3D/주봉 아래꼬리 비율 < 30% (계단식 X)
WICK_LOOKBACK_BARS = 6           # 최근 N개 봉의 아래꼬리 평균 평가
FUNDING_1H_MIN = -0.0001         # 1H 펀비 > -0.01% (1주기, 일반적으로 1H은 0.01% 단위)
FUNDING_8H_MIN = -0.0008         # 8H 펀비 (바이낸스 표준 주기) > -0.08%
LIQ_CLUSTER_MIN_DIST = 0.03      # 위쪽 청산 클러스터 ≥ 3% (Coinglass 연동시)
MIN_ABS_BB_DEVIATION = 0.015     # 절대 BB이격 ≥ 1.5%. 백분위만 높고 절대값 작은 시그널 차단
                                 # (익절 거리 < 펀비 비용 케이스 회피)

# ============================================================
# 종목 선택
# ============================================================
TOP_N_BY_VOLUME = 200            # 24시간 거래대금 상위 N개만 스캔
MIN_24H_VOLUME_USD = 5_000_000   # 최소 거래대금 (저시총 회피)
EXCLUDE_SYMBOLS = [              # 강제 제외 종목 (룰북 §7-1 ST 종목 등)
    # "BTCUSDT", "ETHUSDT",  # 메인은 역하이엔드 대상이라 제외 가능
]
EXCLUDE_KEYWORDS = ["BTCDOM", "DEFI", "USDC"]  # 인덱스/스테이블 제외

# Deadking zone 블랙리스트
# 운영자가 'Deadking zone' 또는 'Deadking zone OS' 로 발사한 종목.
# 이 등급은 단타 단발성으로, 같은 종목 반복 진입은 손실 누적이 검증된 패턴.
# 새 종목이 Deadking zone 으로 떨어질 때마다 사용자가 수동 추가한다.
# LAB 5/31 Deadking zone 15회+ 발사로 회피 종목 재분류 (RAVE 패턴과 동일)
DEADKING_BLACKLIST = ["RAVE", "LAB"]

# 운영자(강의 채널) 실제 매매 검증 종목 (parse_leading_channel.py 산물)
# 검색기에 신호 떴을 뿐만 아니라 운영자가 실제로 비중 X% 시장가 정리까지 한 자리.
# 진입 후보의 신뢰도 가산점.
# 주의: 같은 종목이 DEADKING_BLACKLIST 에도 있으면 블랙리스트가 우선 (자동 NONE).
# 갱신 이력 (각 종목 운영자 진입 일자 + 보유 시간):
#   STO/NOM/JOE/KOMA/SPK/SAGA/UB/BEAT — 기존 검증 종목 (강의 채널 ACTION)
#   NEAR   5/26 운영자 본인 분석 진입 → 5/29 09:58 완결, 보유 61h (본인 분석 첫 완결)
#   ALLO   5/29 07:56 1차 + 2차 진입, 진행 중 (길어질 수 있는 포지션)
#   XLM    5/29 진입 → 당일 전량 (단타 완결, "반등 가능성" 선제 익절)
#   PORTAL 5/31 07:45 진입 → 09:51 전량, 보유 2h6m (사상 최단 트레이드);
#          6/1 새벽 재펌프 MAD OS
#   LAB 제거 → DEADKING_BLACKLIST 로 이동 (Deadking zone 반복 발사)
#   RAVE 제거 → 이미 DEADKING_BLACKLIST
OPERATOR_TRADED_SYMBOLS = [
    "STO", "NOM", "JOE", "KOMA", "SPK", "SAGA", "UB", "BEAT",
    "NEAR", "ALLO", "XLM", "PORTAL",
]

# ============================================================
# 데이터 페치
# ============================================================
DAILY_LOOKBACK_DAYS = 100        # BB22 계산을 위해 충분히
H4_LOOKBACK_BARS = 50
H1_LOOKBACK_BARS = 50
HISTORICAL_DEV_DAYS = 30         # 백분위 계산용 분포 기간

# ============================================================
# 출력
# ============================================================
OUTPUT_JSON = True               # 결과를 JSON으로도 저장
OUTPUT_DIR = "./output"
LOG_LEVEL = "INFO"               # DEBUG | INFO | WARNING | ERROR

# 텔레그램 알림 (선택)
TELEGRAM_ENABLED = False
TELEGRAM_BOT_TOKEN = ""          # @BotFather에서 발급
TELEGRAM_CHAT_ID = ""            # 자기 채팅 ID 또는 그룹 ID
TELEGRAM_MIN_GRADE = "A"         # 이 등급 이상만 알림 (S+ | S | A | B | C+ | C)

# ============================================================
# 운영
# ============================================================
SCAN_INTERVAL_SEC = 900          # 15분마다 스캔 (룰북 §3-1 15분봉 기준)
CONCURRENT_REQUESTS = 5          # 동시 API 요청 수
REQUEST_TIMEOUT_SEC = 30
RETRY_MAX = 3
