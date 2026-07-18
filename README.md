# Bybit 선물 자동매매 봇

뉴스, 차트(다중 시간프레임 기술적 분석), 폴리마켓 예측시장, 거래량을 종합해 진입 신호를
만들고, 진입 후에는 ATR 기반 손절/익절을 상황에 맞게 계속 조정(추세 지속 시 TP 연장,
SL 트레일링, 급락/급등 시 즉시 탈출)하는 Bybit USDT 무기한선물 자동매매 봇입니다.

**먼저 읽어주세요 (매우 중요)**

- **이 봇은 수익을 보장하지 않습니다.** 뉴스+차트+예측시장을 결합해도 승률을 확정할 수
  있는 알고리즘은 존재하지 않습니다. 소액 계좌(예: $26)를 몇백 배로 불리는 목표는 봇의
  로직이 아니라 레버리지/베팅 크기 문제이며, 그 방향으로 설정을 조정할수록 전액 손실
  확률도 함께 커집니다.
- **현재 `config.yaml` 기본값은 보수적이지 않습니다.** 포지션당 자산의 25%를 증거금으로
  쓰고(`risk.position_size_pct_of_equity`), 종목당 4~10배 레버리지를 쓰며
  (`risk.leverage_by_symbol`), 최대 4개 포지션까지 동시 진입(`risk.max_concurrent_positions`)이
  가능하도록 설정되어 있습니다. 4개가 동시에 진입하면 증거금을 합산 100%까지 쓸 수 있고,
  손절 시 손실률은 더 이상 거래당 고정 %로 제한되지 않습니다 (레버리지 × 손절폭에 따라
  달라짐). 이 값들은 의도적으로 이렇게 설정한 것이니, 감당 가능한 리스크인지 스스로
  다시 한번 확인하세요.
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
render_app.py         Render 배포용: 봇 루프(스레드) + 대시보드를 한 프로세스로 결합
render.yaml           Render Blueprint (서비스/환경변수/디스크 정의)
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
2. **진입 조건**: 동시보유 한도/일일 손실 한도를 넘지 않는 상태에서, 두 갈래로 나뉩니다.
   - **추세추종**: 종합 신호 방향이 중립이 아니고 신뢰도가 `risk.min_confidence_to_enter`
     (기본 0.55) 이상이면 그 방향으로 진입. 신뢰도가 높을수록 레버리지를
     `risk.leverage_by_symbol[종목].max` 쪽으로, 낮을수록 `.min` 쪽으로 사용 (종목별로
     다른 범위 설정 가능, 목록에 없는 종목은 `[1, risk.max_leverage]` 범위 사용).
   - **레인지(횡보) 역추세 단타**: 종합 신호가 **중립(neutral)일 때만** 시도. 최근
     `signals.technical.range_lookback`(기본 20)개 봉의 고점/저점을 구해서, 그 폭이
     `range_trade.max_range_width_atr_mult`×ATR(기본 4배) 이내일 때만 "진짜 횡보"로
     보고 진행합니다 (neutral이라고 다 횡보는 아니라서 — 신호들끼리 상쇄돼서 neutral이
     나온 실제 추세 구간을 걸러내기 위함). 폭이 그보다 넓으면(추세/확장 국면) 레인지
     매매를 시도하지 않습니다. 통과하면 현재가가 저점 근처
     (`range_trade.edge_atr_mult`×ATR 이내)일 때 롱, 고점 근처일 때 숏으로 진입.
     레버리지는 신뢰도와 무관하게 항상 그 종목의 `leverage_by_symbol[종목].max` 사용.
     추세추종 진입과는 겹치지 않도록 상호 배타적으로 동작합니다.
3. **포지션 사이징**: **계좌 자산의 `risk.position_size_pct_of_equity`(기본 25%)를
   증거금으로 쓰고, 거기에 레버리지를 곱한 명목 크기**로 수량을 계산합니다
   (예: 자산 1000 USDT, 25%, 레버리지 10배 → 명목 2500 USDT 포지션). 손절 시 손실액은
   더 이상 자산의 고정 %가 아니라 **레버리지 × 손절폭**에 따라 달라지므로, 레버리지가
   높거나 손절폭이 넓을수록 손절 시 손실률도 커집니다. `risk.max_concurrent_positions`
   (기본 4)개가 동시에 이 비율로 진입하면 증거금을 합산 100%까지 쓸 수 있습니다.
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
   - **정체(횡보) 포기 청산**: 포지션이 `stale_exit_after_min`(기본 60분) 이상 열려있는데
     그 사이 가격이 진입가 대비 `stale_exit_max_move_pct`(기본 0.5%) 이내에서만 움직였으면,
     손익 부호와 상관없이(플러스든 마이너스든) 시장가로 정리하고 그 슬롯을 비웁니다.
     `stale_exit_after_min: 0`으로 두면 이 체크 자체를 끌 수 있습니다.
   - **레인지 트레이드는 트레일링/TP연장을 타지 않습니다**: 손익분기/트레일링/TP연장은
     추세추종 진입에만 적용되고, 레인지 진입은 위 급락탈출/신호반전/정체청산과 자기 자신의
     (좁은) SL/TP로만 종료됩니다 — 원래 "단타"로 설계된 진입이라 더 오래 들고 가지 않습니다.
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

## Render에 배포하기

VPS 대신 [Render](https://render.com)에서 돌리고 싶다면 아래처럼 구성합니다. Render는
서비스당 HTTP 포트를 하나만 열어주고, 서비스끼리 디스크를 공유하지 않기 때문에
(VPS 구성처럼 봇과 대시보드를 별도 프로세스 2개로 나눌 수 없음) `render_app.py`가
**봇 루프를 백그라운드 스레드로 돌리면서 같은 프로세스에서 대시보드를 서빙**하도록
합쳐져 있습니다.

### A. Blueprint로 배포 (권장)

레포에 포함된 `render.yaml`을 그대로 사용합니다.

1. Render 대시보드 → **New** → **Blueprint** → 이 레포 선택 → `render.yaml` 자동 인식.
2. 배포 전에 `sync: false`로 표시된 환경변수를 Render 대시보드에서 직접 채워넣습니다:
   `BYBIT_API_KEY`, `BYBIT_API_SECRET`, `NEWSAPI_KEY`(선택), `TELEGRAM_BOT_TOKEN`(선택),
   `TELEGRAM_CHAT_ID`(선택), `DASHBOARD_TOKEN`(무작위 값으로).
3. `BYBIT_TESTNET`은 기본 `true`. 실거래로 전환하려면 `false`로 바꾸세요.
4. Start Command는 `gunicorn render_app:app --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 120`
   입니다. **gunicorn 워커는 반드시 1개**여야 합니다 (워커가 늘어나면 봇 루프가 프로세스마다
   중복 실행됩니다).

### B. 수동으로 Web Service 생성

Blueprint를 쓰지 않는다면 New → Web Service로 직접 만들고:
- Build Command: `pip install -r requirements.txt`
- Start Command: `gunicorn render_app:app --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 120`
- 위 3.의 환경변수들을 동일하게 설정.

### C. 플랜 선택: Free + UptimeRobot vs Starter + 디스크

`render.yaml`은 기본값이 **`plan: free`** 입니다. Free Web Service는 15분간 아무 HTTP
요청도 없으면 슬립되고, 슬립되는 순간 봇 루프도 같이 멈춥니다. 이를 막으려면
[UptimeRobot](https://uptimerobot.com) 같은 외부 핑 서비스로 슬립되기 전에 주기적으로
요청을 보내야 합니다 (아래 E 참고). 두 방식의 트레이드오프:

| | **Free + UptimeRobot** | **Starter + 영구 디스크** |
|---|---|---|
| 비용 | 무료 | 최소 $7/월 |
| 슬립 방지 | 핑이 15분 내에 계속 도착해야 함 (실패하면 슬립 → 재기동) | 애초에 슬립 안 함 |
| 상태 영속성 | **없음.** 슬립되거나(핑 실패/지연), 재배포되거나, 크래시 나면 `data/state.json`·`logs/`가 초기화됨 | 영구 디스크 사용 시 재배포/재시작에도 유지됨 |
| 월 사용량 한도 | Render 계정 전체에서 무료 인스턴스 750시간/월 공유 (24시간 서비스 하나만 있어도 거의 꽉 참) | 한도 없음 |

**Free + UptimeRobot을 선택했다면** (지금 `render.yaml` 기본값) 다음을 감안하세요:
- 실제 포지션의 SL/TP는 항상 Bybit 거래소 서버에 주문으로 걸려 있으므로, 봇 프로세스가
  멈춰도(슬립/재기동 중) 청산 자체는 거래소가 대신 집행합니다.
- 하지만 **"일일 손실 한도 도달 시 신규 진입 중단"** 로직은 `data/state.json`의
  `daily.realized_pnl`을 기준으로 판단하는데, 슬립 후 재기동되면 이 값이 사라져 한도가
  리셋됩니다. 즉 한도 보호가 완벽하지 않을 수 있습니다.
- 테스트넷 검증 단계나 소액 운영에는 괜찮지만, 실거래 규모를 키운다면 Starter로
  전환하고 영구 디스크를 붙이는 걸 권장합니다. 전환하려면 `render.yaml`에서
  `plan: free`를 `plan: starter`로 바꾸고 아래를 추가하면 됩니다:
  ```yaml
      envVars:
        - key: DATA_DIR
          value: /var/data/data
        - key: LOG_DIR
          value: /var/data/logs
      disk:
        name: bybit-bot-data
        mountPath: /var/data
        sizeGB: 1
  ```

### D. 대시보드 접속

배포된 서비스 URL의 `/?token=<DASHBOARD_TOKEN>`으로 접속합니다. 예:
`https://bybit-bot.onrender.com/?token=...`

### E. UptimeRobot으로 슬립 방지 (Free 플랜)

1. [uptimerobot.com](https://uptimerobot.com)에서 무료 계정 생성.
2. **Add New Monitor** → Monitor Type: `HTTP(s)`.
3. URL: `https://<서비스이름>.onrender.com/healthz` (대시보드 `/`가 아니라 반드시
   `/healthz`를 사용하세요 - 토큰 없이 200을 반환하는 전용 헬스체크 경로라 오탐도 없고
   Bybit API를 호출하지 않아 가볍습니다).
4. Monitoring Interval: **5분** (UptimeRobot 무료 플랜 최소 간격이 5분이라, 요청하신
   14분도 문제없이 설정 가능하지만 Render의 15분 슬립 기준을 안전하게 피하려면 5~10분
   간격을 권장합니다. 14분으로 하면 Render 쪽 타이밍/지연에 따라 간발의 차로 슬립되는
   경우가 생길 수 있습니다).
5. 저장 후 UptimeRobot의 Response Time 그래프에서 계속 200이 찍히는지 확인하세요.
   한 번이라도 슬립되어 Render가 콜드스타트하면 그 응답은 지연되거나 실패로 찍힐 수
   있습니다 (콜드스타트는 보통 수십 초 소요).

---

## 설정 커스터마이즈 (`config.yaml`)

| 항목 | 의미 |
|---|---|
| `exchange.symbols` | 매매할 종목 목록 |
| `risk.position_size_pct_of_equity` | 포지션당 증거금으로 쓸 자산 비율 (여기에 레버리지를 곱한 게 명목 포지션 크기) |
| `risk.max_leverage` | `leverage_by_symbol`에 없는 종목의 레버리지 상한 (하한은 1) |
| `risk.leverage_by_symbol` | 종목별 `{min, max}` 레버리지 범위. 신뢰도에 따라 그 범위 내에서 보간 |
| `risk.max_concurrent_positions` | 동시에 보유 가능한 포지션(종목) 최대 개수 |
| `risk.max_daily_loss_pct` | 이 손실률에 도달하면 당일 신규 진입 중단 |
| `risk.min_confidence_to_enter` | 이 신뢰도 미만이면 진입 안 함 (높일수록 거래 빈도↓ 확신도↑) |
| `signals.weights` | 기술적/거래량/뉴스/폴리마켓 각 신호의 반영 비중 |
| `signals.technical.range_lookback` | 레인지 고점/저점을 구할 때 볼 최근 봉 개수 |
| `trade_management.*` | 손절/익절/트레일링/TP연장/긴급탈출 세부 파라미터 |
| `trade_management.stale_exit_after_min` / `stale_exit_max_move_pct` | 이 시간 이상 열려있는데 가격이 이 %만큼도 안 움직였으면(횡보) 손익 무관 정리. 0으로 끄기 가능 |
| `trade_management.range_trade.*` | 신호 중립일 때만 시도하는 레인지 역추세 단타 진입의 세부 파라미터 (`enabled`, `edge_atr_mult`, `max_range_width_atr_mult`, 자체 `atr_sl_multiplier`/`atr_tp_multiplier`) |
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
