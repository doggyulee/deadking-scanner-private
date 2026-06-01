# 데드킹 하이엔드 스캐너 (Deadking Scanner)

> 데드킹 하이엔드 기법 마스터 룰북 (1주차+2주차+3주차) 을 기반으로 한
> 코인 숏 시그널 스캐너. Phase 2 자동 필터 시스템.

## 🎯 무엇을 하는가

전 세계 USDT 무기한 선물 시장을 15분마다 스캔해서, 룰북에 정의된
**6등급 시그널 시스템**으로 분류하고 **7가지 PASS 필터**를 자동 적용한다.

- ✅ 일봉 BB(22,2) 이격도 → 종목별 30일 분포 기준 백분위 (운영자 인정 "종목마다 다르다" 자동화)
- ✅ 다중 TF RSI(6) (1D / 4H / 1H) — 99 극단치는 자동 MAD 격상
- ✅ 5MA 이격도, 펀비, ATH, 30일 고점, 아래꼬리 비율 모두 자동 체크
- ✅ 텔레그램 알림 (등급 임계값 설정 가능)
- ✅ JSON 결과 저장 (백테스트/통계 분석용)

## 📊 시그널 6등급 (룰북 §3 기준)

| 등급 | 신호 | 조건 | 권장 비중 |
|------|------|------|-----------|
| **S+** | Good Short Zone MAD OS | 굿쇼존 + 미친 오버슈팅 | 0.4% |
| **S** | Good Short Zone OS | 굿쇼존 + 오버슈팅 | 0.2% |
| **A** | Good Short Zone | 이격 90+ 퍼센타일 | 0.1% |
| **B** | Monitoring MAD OS | 모니터링 + 미친 오버슈팅 | 0.1% |
| **C+** | Monitoring OS | 모니터링 + 오버슈팅 | 0.05% |
| C | Monitoring | 숏존 진입 (와치만) | — |

**진입 권장**: A 등급 이상 + 7가지 필터 모두 PASS

## 🛡️ 7가지 PASS 필터 (룰북 §4-1, §7-1)

진입 후보가 되려면 모두 통과해야 함:

1. **ATH 필터**: 현재가 < 상장 이래 ATH × 0.95
2. **30일 고점**: 최근 30일 고점 미돌파
3. **아래꼬리 비율**: 일봉 평균 < 30% (계단식 상승 / 자전거래 차단)
4. **펀비 필터**: 음펀비가 너무 세면 차단 (룰북: 1H -1% 이상, 4H -2% 이상)
5. **이격도 필터**: BB 이격 ≥ 종목별 30일 분포 90 퍼센타일
6. **청산맵 필터**: 위쪽 청산 클러스터 거리 ≥ 3% (Coinglass 연동 시)
7. **ST 필터**: 상장폐지 임박/비활성 종목 차단

## 🚀 설치 & 실행

### 1. 의존성 설치

```bash
cd deadking_scanner
pip install -r requirements.txt
```

필요 패키지: `ccxt`, `pandas`, `numpy` 만. API 키 불필요 (공개 데이터만 사용).

### 2. 자체 검증 (선택)

거래소 연결 없이 분류기/필터 동작 확인:

```bash
python self_test.py
```

### 3. 1회 스캔

```bash
python -m deadking_scanner
```

거래대금 상위 200개 USDT 무기한 종목을 스캔하고 결과 출력 + `output/scan_*.json` 저장.

### 4. 반복 스캔 (15분 주기)

```bash
python -m deadking_scanner --loop
```

### 5. 자주 쓰는 옵션

```bash
# S 등급 이상만 보기
python -m deadking_scanner --min-grade S

# 거래대금 상위 100개만
python -m deadking_scanner --top 100

# 비트겟 사용
python -m deadking_scanner --exchange bitget

# JSON 저장 끄기
python -m deadking_scanner --no-json
```

## 📱 텔레그램 알림 설정

1. `@BotFather` 에서 봇 생성 → 토큰 받기
2. 자기 봇과 대화 시작 → `https://api.telegram.org/bot<TOKEN>/getUpdates` 에서 `chat_id` 확인
3. `config.py` 수정:

```python
TELEGRAM_ENABLED = True
TELEGRAM_BOT_TOKEN = "1234567890:AAExxx..."
TELEGRAM_CHAT_ID = "123456789"
TELEGRAM_MIN_GRADE = "A"   # A 등급 이상만 알림
```

또는 환경변수로:

```bash
export TELEGRAM_BOT_TOKEN="..."
export TELEGRAM_CHAT_ID="..."
```

## ⚙️ 임계값 튜닝 (룰북 §11-1 운영자 본인 인정 "종목마다 다르다" → 백분위로 해결)

`config.py` 에서 조정:

```python
MONITORING_PCTL = 70       # 70 → 80 으로 올리면 더 보수적
GOOD_SHORT_PCTL = 90       # 90 → 95 로 올리면 굿쇼존이 더 까다로워짐
OS_DEVIATION = 0.005       # 0.5% → 1.0% 로 올리면 OS 조건 강화
MAD_OS_DEVIATION = 0.025   # 2.5% → 5.0% 로 올리면 MAD OS 조건 강화
```

룰북에서 운영자는 정확한 % 수치를 안 주고 "종목마다 다르다"고 함.
이 스캐너는 종목별 30일 BB 이격 분포의 백분위로 자동 정규화하므로
어떤 종목이든 일관된 기준 적용.

## 📁 파일 구조

```
deadking_scanner/
├── README.md              # 이 문서
├── requirements.txt       # ccxt, pandas, numpy, telethon, python-dotenv
├── .env.example           # 텔레그램 자격증명 템플릿 (.env 는 gitignore)
├── .gitignore             # .env / *.session / output/ 제외
├── config.py              # 모든 임계값 (룰북 기준)
├── indicators.py          # BB(22,2), RSI(6), MA, 이격도, 아래꼬리
├── exchange.py            # ccxt 래퍼 (심볼/OHLCV/펀비/ATH)
├── filters.py             # 7가지 PASS 필터
├── classifier.py          # 6등급 시그널 분류
├── telegram_notifier.py   # 텔레그램 알림 (봇 API, 송신)
├── telegram_listener.py   # Phase 1 — 검색기+강의 2채널 실시간 수신 (Telethon)
├── parse_leading_channel.py # 강의 채널 HTML 익스포트 → trades.jsonl (classify 공유)
├── match_analyzer.py      # 운영자 신호 vs 우리 스캐너 매칭률 분석
├── scanner.py             # 메인 스캔 파이프라인
├── __main__.py            # CLI 진입점
├── self_test.py           # 오프라인 자체 검증
└── output/                # 스캔 결과 JSON / operator_signals.jsonl / operator_trades.jsonl
```

## 📡 Phase 1 — 운영자 채널 리스너 (`telegram_listener.py`, 2채널 동시 가동)

운영자 개인 채널(username 없음) **두 개를 동시에** 실시간 수신한다:

| 채널 | `.env` 이름 변수 | 저장 파일 | 파싱 |
|------|------------------|-----------|------|
| **검색기** | `TELEGRAM_OPERATOR_CHANNEL_NAME` | `output/operator_signals.jsonl` | 등급/심볼/BB·5MA 이격/태그 (`parse_message`) |
| **강의** | `TELEGRAM_LECTURE_CHANNEL_NAME` | `output/operator_trades.jsonl` | ACTION/BRIEFING 등 (`parse_leading_channel.classify`) |

- **`operator_signals.jsonl` (검색기)** — 운영자 검색기가 발사하는 *신호*. `{timestamp, grade,
  symbol, bb_pct, ma5_pct, tags, raw_text, message_id, channel_id}`. 우리 스캐너 분류 결과와
  일치율을 추적 → 임계값 튜닝의 정답지.
- **`operator_trades.jsonl` (강의)** — 운영자가 강의 채널에 직접 쏘는 *실매매 액션·브리핑*.
  `{timestamp, kind, ...필드, raw, message_id, channel_id}`. `kind` 는
  `ACTION`(`## 비중 X%`) / `ACTION_OUT`(전량) / `BRIEFING`(종목명·관찰구간·3분할) /
  `MONITORING_SHARE` / `ANNOUNCE` / `UNCLASSIFIED`. 매칭 안 되는 메시지도 `UNCLASSIFIED` 로
  raw 보존. 파싱 로직은 `parse_leading_channel.py` 와 단일 소스를 공유한다.

콘솔 출력에는 `[검색기]` / `[강의]` 접두사로 어느 채널 메시지인지 표시한다.
강의 채널 변수(`TELEGRAM_LECTURE_CHANNEL_NAME`)를 비워두면 검색기 채널만 모니터링한다.

### 1. API 자격증명 발급

`my.telegram.org` → API development tools 에서 본인 계정으로 `api_id`, `api_hash` 발급.
(봇이 아니라 **사용자 계정**으로 로그인해야 개인 채널 메시지를 받을 수 있다.)

### 2. `.env` 채우기

`.env.example` 를 보고 같은 디렉터리에 `.env` 작성:

```env
TELEGRAM_API_ID=12345678
TELEGRAM_API_HASH=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TELEGRAM_OPERATOR_CHANNEL_NAME=데드킹 하이엔드 검색기
TELEGRAM_OPERATOR_CHANNEL_ID=
TELEGRAM_LECTURE_CHANNEL_NAME=[데드킹] 코인 하이엔드 강의
TELEGRAM_LECTURE_CHANNEL_ID=
```

`*_CHANNEL_ID` 두 개는 비워둔다 — 첫 실행 시 이름으로 자동 탐색해 채워줌.

> ⚠️ **보안 주의**
> - `.env` 와 `*.session` 은 `.gitignore` 로 이미 제외돼 있다. 절대 커밋 금지.
> - `*.session` 안에는 텔레그램 로그인 토큰이 있어 노출되면 계정 탈취 가능.
> - 의심되면 텔레그램 앱 → 설정 → 활성 세션에서 즉시 강제 종료 가능.

### 3. 실행

```bash
pip install -r requirements.txt
python -X utf8 telegram_listener.py
```

첫 실행:
1. 텔레그램 인증 코드 입력 (앱으로 받음, 2FA 비번 있으면 그것도)
2. 가입된 채널들 순회하며 검색기·강의 두 이름 모두 매칭 → 발견 시 각 ID 를 `.env` 에 자동 저장
3. 못 찾으면 가입 채널 목록 출력 → `.env` 의 해당 `*_CHANNEL_NAME` 수정 후 재실행

이후 실행은 저장된 ID 로 바로 두 채널 리스닝.
시작 시 "📋 리스닝 대상" 에 검색기·강의 두 채널이 모두 표시되면 정상 가동.

### 4. 매칭 분석

운영자 신호가 몇 건 쌓이고 스캐너도 같은 시간대에 돌고 있어야 한다.

```bash
python match_analyzer.py                     # 누적 전체
python match_analyzer.py --since 2026-05-24  # 특정 날짜부터
python match_analyzer.py --window 30         # ±30 분 윈도우
```

운영자 신호 발사 시각 ±15분 안에 떨어진 가장 가까운 `scan_*.json` 을 골라
같은 종목의 zone/grade 를 비교 → 일별 매칭률 출력.

## 🔬 출력 예시

```
========================================================================
📊 등급별 집계
========================================================================
  😈😈😈🔥 S+  (  2개)  XRP, DOGE
  😈🔥    S   (  5개)  SOL, ADA, LINK, AVAX, DOT
  🔥      A   ( 12개)  ATOM, FIL, NEAR, ...
  😈      C+  ( 18개)  ...
  👀      C   ( 35개)  ...

========================================================================
🎯 진입 가능 시그널 (A 등급 이상)
========================================================================

😈😈😈🔥 [S+] XRP/USDT  →  Good Short Zone MAD OS
   가격: 0.6234  |  권장비중: 0.40%
   BB상단: 0.5987  |  이격: +4.12% (97퍼센타일)
   RSI(6): 1D=92.3 / 4H=99.1 / 1H=99.4
   펀비: 0.0123% / 8h
   필터: ✅ ALL PASS
```

## 🚧 한계 (의도적 / 다음 단계)

- **자동 매매 미포함** — 이 스캐너는 신호 발사까지만. 룰북 Phase 4 (비트겟 API 자동 매매)는 별도 모듈 필요. 신호 받아서 거미줄 자동 등록하는 부분은 다음 빌드.
- **Coinglass 청산맵 미연동** — 위쪽 클러스터 거리 필터는 현재 PASS로 처리. 비공식 API 또는 스크린샷 OCR 연동은 다음 빌드.
- **하이엔드 데드캣 / 딜레이 데드캣 자동 감지 미포함** — 룰북 §8-1, §8-2 신규 기법. 추세 이탈 + 말아올림 캔들 검출 로직 필요. 다음 빌드.
- **역하이엔드 (롱) 미포함** — 룰북 §9. BTC/ETH 주봉/월봉 BB 하단 감시는 별도 스캐너로 분리 권장 (다른 시간프레임 / 다른 종목군).

## 📌 룰북 매핑 (어느 룰북 항목이 어디에 구현됐는가)

| 룰북 항목 | 구현 위치 |
|----------|----------|
| §2 지표 설정 (BB 22, RSI 6, 5MA) | `config.py` + `indicators.py` |
| §3 6등급 시그널 시스템 | `classifier.py` |
| §3-1 15분봉 기준 | `config.SCAN_INTERVAL_SEC = 900` |
| §3-2 등급별 비중 | `classifier.GRADE_WEIGHTS` |
| §4-1 7가지 PASS 필터 | `filters.py` |
| §4-2 진입 강도 결정 | `classifier.classify()` |
| §7-1 진입 전 체크 | `filters.run_all_filters()` |
| §8-3 RSI 99 단타 | `classifier` MAD 격상 로직 |
| §11-3 운영자 검색기와 차별화 | 종목별 30일 분포 백분위 (운영자: 절대값) |

## 🧠 운영자 검색기 대비 알파 (룰북 §11-3)

운영자 검색기에 없는 기능을 너만 갖는다:

- ✅ **종목별 동적 임계값** — 운영자는 절대 이격도, 너는 30일 분포 95퍼센타일
- ✅ **다중 TF RSI 자동 격상** — 4H+1H RSI 99 동시 → MAD 강제 격상
- ✅ **펀비 함정 자동 차단** — 1H 환산 + 8H 표준 둘 다 체크
- ✅ **아래꼬리 비율 자동 계산** — 계단식 상승 자동 회피
- ✅ **JSON 로깅** — 모든 신호 기록 → 통계로 임계값 fine-tuning

추가 예정:
- 청산맵 거리 (Coinglass 연동)
- OI 변화율 추적 (1H +10% 자동 감지)
- 다중 신호 채널 컨센서스 (운영자 채널 + 자체 검색기 일치도)

## 📜 라이선스 / 면책

자체 사용 목적. 데드킹 강의 내용 기반 룰북은 본인 소유. 매매 책임 본인.
운영자 멘트 인용: **"리스크 우선, 수익은 나중. 경험치만이 진짜 자산."**
