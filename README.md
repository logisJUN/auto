# Bybit 선물 자동매매 봇

뉴스, 차트(다중 시간프레임 기술적 분석), 폴리마켓 예측시장, 거래량을 종합해 진입 신호를
만들고, 진입 후에는 ATR 기반 손절/익절을 상황에 맞게 계속 조정(추세 지속 시 TP 연장,
SL 트레일링, 급락/급등 시 즉시 탈출)하는 Bybit USDT 무기한선물 자동매매 봇입니다.

**먼저 읽어주세요 (매우 중요)**

- **이 봇은 수익을 보장하지 않습니다.** 뉴스+차트+예측시장을 결합해도 승률을 확정할 수
  있는 알고리즘은 존재하지 않습니다. 소액 계좌(예: $26)를 몇백 배로 불리는 목표는 봇의
  로직이 아니라 레버리지/베팅 크기 문제이며, 그 방향으로 설정을 조정할수록 전액 손실
  확률도 함께 커집니다. `config.yaml`의 리스크 값은 "많이 벌기"가 아니라 "오래 살아남기"에
  맞춰진 보수적인 기본값입니다.
- **아이패드에서 24시간 "직접" 실행하는 것은 iOS 특성상 불가능합니다.** iPad는 앱을
  백그라운드에서 영구 실행할 수 없습니다. 그래서 이 봇은 **저렴한 VPS(클라우드 서버)에서
  24시간 돌아가고, 아이패드는 Safari 브라우저(모니터링 대시보드)나 SSH 앱으로 접속해서
  확인/제어하는 구조**로 설계했습니다. (아래 "iPad에서 운영하기" 참고)
- **반드시 테스트넷(모의투자)으로 먼저 충분히 돌려보고, 실제 자금은 감당 가능한 손실
  범위 내에서만 사용하세요.** `.env`의 `BYBIT_TESTNET=true`가 기본값입니다.

---

## 구조

```
bot/
  config.py          .env(비밀키) + config.yaml(전략 파라미터) 로딩
  exchange/
    bybit_client.py  Bybit V5 API 래퍼 (시세, 잔고, 주문, SL/TP 설정)
  signals/
    technical.py     다중 시간프레임 RSI/EMA/MACD/볼린저/ATR/거래량 스코어
    news.py          무료 RSS(코인데스크 등) + 선택적 NewsAPI 뉴스 감성 스코어
    polymarket.py    Polymarket 예측시장 확률/모멘텀 스코어
    aggregator.py    위 신호들을 가중 결합 -> 방향/신뢰도
  risk/
    position_sizing.py  계좌 자산·리스크%·손절폭 기반 수량 계산
    stop_manager.py      초기 SL/TP, 손익분기 이동, 트레일링, TP 연장, 급락/급등 긴급탈출
  strategy.py        진입/청산 판단 및 포지션 관리 오케스트레이션
  main.py            메인 루프 (entry point)
dashboard/app.py     아이패드에서 볼 수 있는 읽기 전용 모니터링 웹페이지
deploy/              VPS에 24시간 서비스로 등록하기 위한 systemd 유닛 파일
tests/               핵심 로직(사이징, 손절/익절, 신호) 단위 테스트
```

## 매매 로직 요약

1. **신호 수집 (2분마다 재계산, 캐시됨)**
   - 기술적 분석: 15분/1시간/4시간봉의 EMA 추세, RSI 모멘텀, MACD, 볼린저밴드 위치를
     ATR로 정규화해 종목별 스코어(-1~1)로 환산. 긴 시간프레임일수록 더 큰 가중치.
   - 거래량: 최근 거래량이 평균 대비 급증했는지, 그 방향이 가격 방향과 일치하는지.
   - 뉴스: RSS 피드(코인데스크/코인텔레그래프/크립토슬레이트)에서 최근 기사 제목을
     불리시/베어리시 키워드 사전으로 채점. `NEWSAPI_KEY`를 넣으면 커버리지 확대.
   - 폴리마켓: 비트코인/이더리움/연준/침체 관련 활성 마켓의 "Yes" 확률 수준과, 직전
     갱신 대비 확률 변화(모멘텀)를 방향성 힌트로 사용.
   - 위 4개를 `config.yaml`의 `signals.weights`로 가중 평균. 뉴스/폴리마켓은 관련
     데이터가 적으면(신뢰도 낮음) 자동으로 영향력이 줄어듭니다.
2. **진입 조건**: 종합 신호의 방향이 중립이 아니고, 신뢰도가
   `risk.min_confidence_to_enter`(기본 0.55) 이상이며, 동시보유 한도/일일 손실 한도를
   넘지 않을 때만 진입. 신뢰도가 높을수록 레버리지를 `risk.max_leverage` 한도 내에서
   더 많이 사용.
3. **포지션 사이징**: 레버리지가 아니라 **손절가에 닿았을 때 잃는 금액이 계좌 자산의
   `risk.risk_per_trade_pct`(기본 1.5%)를 넘지 않도록** 수량을 계산합니다. 그 수량이
   요구하는 레버리지가 `risk.max_leverage`를 넘으면 레버리지 한도에 맞춰 수량을 줄입니다.
4. **초기 손절/익절**: 진입 시점 ATR(변동성) 기반. `atr_sl_multiplier`,
   `atr_tp_multiplier`로 조절.
5. **포지션 관리 (8~20초마다)**:
   - **급락/급등 긴급탈출**: 최근 `flash_move_window_sec`(기본 45초) 이내에 포지션에
     불리한 방향으로 `flash_move_pct`(기본 1.2%) 이상 움직이면, 손절가 도달 여부와
     무관하게 즉시 시장가로 청산합니다. (롱인데 급락하는 상황 등)
   - **손익분기/트레일링**: 수익이 초기 리스크의 0.5배(R)를 넘으면 손절가를 진입가로
     이동(본전 확보), 1배(R)를 넘으면 ATR 기반으로 손절가를 계속 유리하게(만) 끌어올림.
   - **TP 연장**: 가격이 익절가에 근접했는데도 신호가 여전히 같은 방향으로 강하게
     (신뢰도·스코어 모두 높게) 유지되면, 즉시 익절하지 않고 ATR만큼 익절가를 연장합니다.
     (최대 `max_tp_extensions`회, 기본 3회 - 무한정 안 먹고 버티지 않도록 제한)
   - **신호 반전 조기청산**: 신호가 포지션 반대 방향으로 강하게(스코어/신뢰도 모두 기준
     이상) 바뀌면 익절/손절 전이라도 조기 청산.
   - 실제 SL/TP는 항상 Bybit 거래소 서버에 주문으로 걸려 있으므로, 봇 프로세스가
     잠깐 멈춰도 거래소가 손절/익절을 대신 집행합니다.
6. **일일 손실 한도**: 하루(UTC 기준) 실현손실이 `risk.max_daily_loss_pct`(기본 8%)를
   넘으면 그날은 신규 진입을 멈춥니다(자정 UTC에 리셋).

---

## 시작하기

### 1) Bybit API 키 발급

Bybit 계정 → API 관리 → 새 API 키 생성 시:
- 권한은 **"Contract Trading"(계약 거래)만** 체크하세요.
- **출금(Withdrawal) 권한은 절대 켜지 마세요.**
- 먼저 [Bybit 테스트넷](https://testnet.bybit.com)에서 테스트용 키를 만들어 충분히
  검증한 뒤 실전 키로 넘어가는 것을 강력히 권장합니다.

### 2) 로컬/서버에서 설치

```bash
git clone <이 레포> auto && cd auto
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# .env를 열어 BYBIT_API_KEY / BYBIT_API_SECRET 등을 채워넣기
```

`config.yaml`에서 매매 종목(`exchange.symbols`), 리스크 비율, 신호 가중치 등을
취향에 맞게 조정할 수 있습니다.

### 3) 테스트 실행 (네트워크 없이 핵심 로직 검증)

```bash
pip install -r requirements-dev.txt
pytest -q
```

### 4) 봇 실행

```bash
python -m bot.main
```

`.env`의 `BYBIT_TESTNET=true`인 동안은 실제 자금이 아닌 Bybit 테스트넷에서만
거래됩니다. 로그는 `logs/bot.log`, 판단 근거는 `logs/decisions.jsonl`에 계속 쌓입니다.

---

## 24시간 운영 + 아이패드에서 모니터링하기

아이패드는 봇을 직접 실행하는 용도가 아니라 **VPS에서 돌아가는 봇을 확인/제어하는
용도**로 씁니다.

### A. VPS 준비 (예: Oracle Cloud 무료 티어, AWS Lightsail, Vultr 등 월 4~6천원대도 충분)

Ubuntu 22.04 기준:

```bash
sudo apt update && sudo apt install -y python3-venv git
git clone <이 레포> auto && cd auto
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # 채워넣기
```

### B. systemd로 24시간 자동 재시작 등록

`deploy/bybit-bot.service`, `deploy/dashboard.service`의 `YOUR_USER`와 경로를
실제 값으로 바꾼 뒤:

```bash
sudo cp deploy/bybit-bot.service deploy/dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now bybit-bot dashboard
sudo systemctl status bybit-bot   # 정상 기동 확인
journalctl -u bybit-bot -f        # 실시간 로그
```

서버가 재부팅되거나 봇이 죽어도 systemd가 자동 재시작합니다.

### C. 아이패드에서 확인하기

1. **모니터링 대시보드 (권장)**: 아이패드 Safari에서
   `http://<서버IP>:8080/?token=<DASHBOARD_TOKEN>` 접속. 계좌 자산, 오늘 손익,
   보유 포지션(진입가/현재가/SL/TP/미실현손익), 최근 판단 로그를 20초마다
   자동 새로고침으로 보여줍니다. `DASHBOARD_TOKEN`은 `.env`에서 무작위 값으로
   바꿔두세요. 외부에 완전히 열어두는 게 꺼려지면 VPS 방화벽에서 본인 IP만
   허용하거나 Tailscale 같은 사설 VPN으로 접속하는 것을 권장합니다.
2. **직접 제어가 필요할 때**: [Termius](https://termius.com) 같은 SSH 앱을
   아이패드에 설치해 서버에 접속, `systemctl stop/start/restart bybit-bot`으로
   봇을 멈추거나 재시작할 수 있습니다.
3. **푸시 알림 (선택)**: `.env`에 `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`를
   설정하면 진입/청산/긴급탈출 시마다 텔레그램으로 알림이 옵니다. 대시보드를
   계속 열어두지 않아도 잠금화면 알림으로 확인 가능합니다.

---

## 설정 커스터마이즈 (`config.yaml`)

| 항목 | 의미 |
|---|---|
| `exchange.symbols` | 매매할 종목 목록 |
| `risk.risk_per_trade_pct` | 거래당 손절 시 잃을 자산 비율 |
| `risk.max_leverage` | 절대 넘지 않을 레버리지 상한 |
| `risk.max_daily_loss_pct` | 이 손실률에 도달하면 당일 신규 진입 중단 |
| `risk.min_confidence_to_enter` | 이 신뢰도 미만이면 진입 안 함 (높일수록 거래 빈도↓ 확신도↑) |
| `signals.weights` | 기술적/거래량/뉴스/폴리마켓 각 신호의 반영 비중 |
| `trade_management.*` | 손절/익절/트레일링/TP연장/긴급탈출 세부 파라미터 |
| `loop.fast_poll_sec` / `idle_poll_sec` | 포지션 보유 중 / 미보유 시 확인 주기 |

---

## 한계와 주의사항

- 뉴스 신호는 유료 감성분석 API 없이 키워드 사전 기반이라 단순합니다. 정교함이
  필요하면 `NEWSAPI_KEY`를 설정해 커버리지를 넓히거나 `bot/signals/news.py`의
  사전을 확장하세요.
- 폴리마켓 마켓들은 "다음 1시간 방향"이 아니라 대개 장기 이벤트를 다루므로,
  단기 매매 신호라기보다 거시적 리스크 심리 지표로 취급했습니다.
- 백테스트 모듈은 포함되어 있지 않습니다. 실전 투입 전 테스트넷에서 최소 몇 주간
  운영해 `logs/decisions.jsonl`과 대시보드의 실현손익 흐름을 직접 검토하세요.
- 거래소 API 키가 유출되면 계좌가 위험해집니다. `.env`는 절대 커밋/공유하지 마세요
  (`.gitignore`에 포함되어 있습니다).
