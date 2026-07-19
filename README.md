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
    position_sizing.py  증거금%·레버리지 기반 수량 계산
    stop_manager.py      초기 SL/TP, 손익분기 이동, 트레일링, TP 연장, 급락/급등 긴급탈출, 정체 청산
    stress_test.py        전종목 동시 손절 가정 시 자산 대비 최대 손실 계산 (대시보드용)
  strategy.py        진입/청산 판단 및 포지션 관리 오케스트레이션
  main.py            메인 루프 (entry point)
dashboard/app.py     아이패드에서 볼 수 있는 읽기 전용 모니터링 웹페이지
deploy/              VPS에 24시간 서비스로 등록하기 위한 systemd 유닛 파일
render_app.py         Render 배포용: 봇 루프(스레드) + 대시보드를 한 프로세스로 결합
render.yaml           Render Blueprint (서비스/환경변수/디스크 정의)
tests/               핵심 로직(사이징, 손절/익절, 신호) 단위 테스트
```

## 매매 로직 요약

0. **감시 종목 구성 (`exchange.universe`, 기본 1시간마다 재스캔)**: 종목을 직접
   고르지 않고, Bybit의 **모든 USDT 무기한선물 티커를 한 번의 가벼운 API 호출**로
   받아서 **24시간 거래대금 상위 `top_n`개**(기본 30개)를 감시 목록으로 자동 구성합니다.
   `exchange.symbols`(기본 `["ETHUSDT"]`)는 스캔 결과와 무관하게 **항상 고정 포함**되는
   종목입니다. 이미 포지션이 열려있는 종목은 다음 스캔에서 목록에서 빠지더라도
   **청산될 때까지 계속 관리**됩니다(SL/TP/트레일링/정체청산 등) — 감시 목록에서
   빠지는 건 "새 진입 후보에서 제외"라는 뜻이지 "관리 중단"이 아닙니다.
   `exchange.universe.enabled: false`로 끄면 예전처럼 `exchange.symbols`에 적은
   종목만 고정으로 거래합니다. 거래대금 상위권이어도 **테이커 수수료율이
   `max_taker_fee_rate`(기본 0.06%)를 넘는 종목은 제외**됩니다 — 신규 상장/저유동성
   종목 중엔 표준(~0.055%)의 2배인 종목이 섞여 있어서, 방향을 맞혀도 수수료만으로
   수익을 깎아먹는 걸 막기 위함입니다. 제외된 만큼은 다음 순위 종목으로 채웁니다.
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
     `risk.leverage_by_symbol[종목].max` 쪽으로, 낮을수록 `.min` 쪽으로 사용. 목록에
     없는 종목(동적 유니버스로 새로 들어온 코인 등)은 `risk.default_leverage_range`
     (기본 5~8배) 사용.
   - **레인지(횡보) 역추세 단타**: 종합 신호가 **중립(neutral)일 때만** 시도. 최근
     `signals.technical.range_lookback`(기본 20)개 봉의 고점/저점을 구해서, 그 폭이
     `range_trade.max_range_width_atr_mult`×ATR(기본 4배) 이내일 때만 "진짜 횡보"로
     보고 진행합니다 (neutral이라고 다 횡보는 아니라서 — 신호들끼리 상쇄돼서 neutral이
     나온 실제 추세 구간을 걸러내기 위함). 폭이 그보다 넓으면(추세/확장 국면) 레인지
     매매를 시도하지 않습니다. 통과하면 현재가가 저점 근처
     (`range_trade.edge_atr_mult`×ATR 이내)일 때 롱, 고점 근처일 때 숏으로 진입.
     레버리지는 신뢰도와 무관하게 항상 그 종목의 `leverage_by_symbol[종목].max` 사용.
     추세추종 진입과는 겹치지 않도록 상호 배타적으로 동작합니다.
3. **포지션 사이징**: **계좌 자산의 `risk.position_size_pct_of_equity`(기본 50%)를
   증거금으로 쓰고, 거기에 레버리지를 곱한 명목 크기**로 수량을 계산합니다
   (예: 자산 1000 USDT, 50%, 레버리지 10배 → 명목 5000 USDT 포지션). 손절 시 손실액은
   더 이상 자산의 고정 %가 아니라 **레버리지 × 손절폭**에 따라 달라지므로, 레버리지가
   높거나 손절폭이 넓을수록 손절 시 손실률도 커집니다.
   - **증거금 버퍼**: 매 진입 전에 이미 열려있는 포지션들이 쓰고 있는 증거금 합계를
     계산해서, 전체 증거금 사용량이 `risk.margin_buffer_pct`(기본 15%)를 뺀 나머지를
     넘지 않도록 이번 진입 증거금을 깎습니다 (필요하면 0으로 만들어 진입 자체를 스킵).
     즉 목표는 항상 50%지만, 다른 포지션들이 이미 많이 차지했으면 그만큼 작게 들어가거나
     아예 안 들어갑니다 — "여유 증거금 부족(ErrCode 110007)" 거부를 줄이고, 계좌에 항상
     최소한의 여윳돈을 남겨두기 위함입니다. **50%(목표) + 15%(버퍼)면 실질적으로 한
     번에 온전한 크기로 들어갈 수 있는 건 2개 포지션(50%+35%) 정도**이고, 나머지는
     아래 "교체 진입"이 없으면 여유 증거금 부족으로 대기합니다.
   - **교체 진입 (override_entry)**: 슬롯이 다 찼거나(`max_concurrent_positions` 도달)
     증거금 여유가 바닥나서 새 진입을 못 하는 상황에서, 새로 들어오려는 신호가
     `override_entry.min_confidence`(기본 0.85) 이상으로 아주 강하고, 지금 보유 중인
     포지션 중 **가장 약한 것**(그 포지션이 열린 방향으로의 *현재* 신뢰도가 가장 낮은
     것 — 신호가 neutral로 식었거나 반대로 뒤집힌 포지션이 최우선 후보)보다
     `override_entry.min_confidence_margin_over_weakest`(기본 0.15) 이상 확실하면,
     그 가장 약한 포지션을 청산하고 바로 이 새 종목으로 들어갑니다. 이 기능이 꺼져있거나
     (`enabled: false`) 조건을 못 만족하면 그냥 대기합니다. 레인지 단타는 방향성 확신도
     개념이 없어서 교체 대상 후보(약한 쪽)로는 들어가지만, 교체를 발동시키는 새 후보로는
     추세추종 진입만 해당합니다.
4. **초기 손절/익절**: 진입 시점 ATR(변동성) 기반. `atr_sl_multiplier`,
   `atr_tp_multiplier`로 조절.
5. **포지션 관리 (8~20초마다)**:
   - **급락/급등 긴급탈출**: 최근 `flash_move_window_sec`(기본 45초) 이내에 포지션에
     불리한 방향으로 일정 % 이상 움직이면, 손절가 도달 여부와 무관하게 즉시 시장가로
     청산합니다. (롱인데 급락하는 상황 등) 이 기준(%)은 고정값이 아니라 **그 포지션의
     진입 시점 ATR에 비례**합니다: `entry_atr / entry_price × 100 × flash_move_atr_mult`
     (기본 배수 1.0), 단 `flash_move_min_pct`(기본 0.8%)~`flash_move_max_pct`(기본 3.0%)
     범위로 제한됩니다. 변동성 큰 저가 알트코인은 기준이 자동으로 넓어지고(최대 3%까지),
     변동성 낮은 코인은 좁아집니다(최소 0.8%까지) — 고정 %였을 때 변동성 큰 코인에서
     진입가 대비 손실이 다 누적된 뒤에야 발동하던 문제(예: ESPORTSUSDT 5%대 손실)를
     줄이기 위함입니다.
   - **손익분기/트레일링**: 수익이 초기 리스크의 0.5배(R)를 넘으면 손절가를 진입가로
     이동(본전 확보), 1배(R)를 넘으면 ATR 기반으로 손절가를 계속 유리하게(만) 끌어올림.
   - **TP 연장**: 가격이 익절가에 근접했는데도 신호가 여전히 같은 방향으로 강하게
     (신뢰도·스코어 모두 높게) 유지되면, 즉시 익절하지 않고 ATR만큼 익절가를 연장합니다.
     (최대 `max_tp_extensions`회, 기본 3회 - 무한정 안 먹고 버티지 않도록 제한)
   - **신호 반전 조기청산**: 신호가 포지션 반대 방향으로 강하게(스코어/신뢰도 모두 기준
     이상) 바뀌면 익절/손절 전이라도 조기 청산.
   - **정체(횡보) 포기 청산**: 포지션이 일정 시간 이상 열려있는데 그 사이 가격이 진입가
     대비 `stale_exit_max_move_pct`(기본 0.5%) 이내에서만 움직였으면, 손익 부호와
     상관없이(플러스든 마이너스든) 시장가로 정리하고 그 슬롯을 비웁니다.
     `stale_exit_after_min: 0`으로 두면 이 체크 자체를 끌 수 있습니다.
     **완전히 안 움직인 포지션을 정리하는 건 왕복 수수료만큼 확정 손실**이라, 슬롯/증거금이
     이미 다 찼을 때만(=그 자금이 실제로 필요할 때만) `stale_exit_after_min`(기본 60분)의
     빠른 기준을 쓰고, 여유가 있을 때는 `stale_exit_after_min_no_pressure`(기본 240분,
     4시간)로 더 여유롭게 기다립니다 — 필요도 없는데 수수료만 내고 정리하는 걸 줄이기
     위함입니다.
   - **레인지 트레이드는 트레일링/TP연장을 타지 않습니다**: 손익분기/트레일링/TP연장은
     추세추종 진입에만 적용되고, 레인지 진입은 위 급락탈출/신호반전/정체청산과 자기 자신의
     (좁은) SL/TP로만 종료됩니다 — 원래 "단타"로 설계된 진입이라 더 오래 들고 가지 않습니다.
   - 실제 SL/TP는 항상 Bybit 거래소 서버에 주문으로 걸려 있으므로, 봇 프로세스가
     잠깐 멈춰도 거래소가 손절/익절을 대신 집행합니다.
   - **SL/TP 미부착 검증 및 복구**: Bybit는 시장가 진입 주문에 `stopLoss`/`takeProfit`을
     같이 실어 보내도, 기본 주문은 체결되고 SL/TP만 조용히 안 걸리는 경우가 있을 수
     있습니다. 그래서 **진입 직후**, 그리고 **매 tick(8~20초)마다** 실제 포지션에 SL/TP가
     걸려있는지 거래소에서 직접 확인합니다. 없으면 `update_trading_stop`으로 재부착을
     시도하고, 그마저 실패하면 무보호 레버리지 포지션을 계속 들고 있는 것보다 안전하다고
     보고 **즉시 시장가로 청산**합니다(텔레그램 알림 발송, 반드시 직접 확인 필요).
   - **추적 안 되는(orphaned) 포지션 회수**: `state.json`이 리셋되거나(예: Render 무료
     플랜에서 슬립 후 재기동, 재배포) 봇 밖에서 직접 연 포지션처럼, 거래소엔 열려있는데
     로컬 상태엔 없는 포지션은 원래 tick()이 아예 쳐다보지도 않아서 SL/TP가 영영
     확인이 안 될 수 있습니다. 그래서 매 tick마다 **거래소의 모든 열린 포지션**을 심볼
     구분 없이 조회해서, 로컬에 없는 게 있으면 기존 SL/TP를 유지하거나(있으면) 위와
     동일하게 검증/복구/청산 절차를 거쳐 관리 대상으로 편입합니다.
   - **실현손익은 거래소 기록 기준**: 청산 직후 Bybit의 청산손익 조회
     (`get_closed_pnl`)에서 실제 체결가·거래 수수료가 반영된 손익을 가져옵니다.
     (구 방식인) "마지막가 - 진입가"로 추정하던 계산은 수수료가 전혀 반영되지 않았는데,
     이제는 거래소가 실제로 집행한 손익 그대로 기록됩니다. 거래소 기록이 아직 안 올라와
     조회에 실패하면 추정치로 폴백하고, 그 경우 `logs/decisions.jsonl`과 대시보드에
     `pnl_is_estimate: true`로 표시됩니다 (수수료 미반영이니 참고만 하세요).
6. **일일 손실 한도**: 하루(UTC 기준) 실현손실이 `risk.max_daily_loss_pct`(기본 8%)를
   넘으면 그날은 신규 진입을 멈춥니다(자정 UTC에 리셋).
   - **한도 기록이 거래소 기준으로 복원됩니다**: `data/state.json`에 저장된 오늘 실현손익
     기록이 초기화되면(Render 무료 플랜의 슬립/재배포 등) 예전엔 그냥 0으로 리셋해서
     한도 보호가 무력화될 수 있었습니다. 지금은 로컬 기록이 오늘 날짜와 안 맞으면
     Bybit의 청산손익 기록에서 **자정(UTC) 이후 실제로 실현된 손익을 다시 계산**해서
     복원합니다 — 정상적인 하루 시작(0으로 시작)과 리셋 후 복구(실제 손익으로 복원) 둘 다
     이 방식으로 정확하게 처리됩니다.

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
   - **리스크 스트레스 테스트** 카드: 지금 설정(종목별 최대 레버리지·목표 증거금%·SL폭)
     기준으로 "전종목이 동시에 손절되면 자산의 몇 %를 잃는지"를 실시간 ATR로 계산해
     보여줍니다. `risk.max_daily_loss_pct`를 넘을 것 같으면 빨간색으로 경고합니다.
     Bybit API 호출이 필요해서 5분마다만 갱신됩니다.
   - **성과 리뷰** 카드: `logs/decisions.jsonl`의 진입/청산 기록을 짝지어 전체 승률·손익과,
     추세추종 vs 레인지 단타, 신뢰도 구간별 승률을 보여줍니다 — "신뢰도가 실제로 잘
     맞았는지"를 사후에 검증하는 용도입니다. 로그가 초기화되면(재배포 등) 같이 리셋됩니다.
2. **직접 제어가 필요할 때**: [Termius](https://termius.com) 같은 SSH 앱을
   아이패드에 설치해 서버에 접속, `systemctl stop/start/restart bybit-bot`으로
   봇을 멈추거나 재시작할 수 있습니다.
3. **푸시 알림 (선택)**: `.env`에 `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`를
   설정하면 진입/청산/긴급탈출 시마다 텔레그램으로 알림이 옵니다. 대시보드를
   계속 열어두지 않아도 잠금화면 알림으로 확인 가능합니다.
4. **일일 요약 이메일 (선택)**: `.env`에 `EMAIL_SMTP_HOST`/`EMAIL_SMTP_PORT`/
   `EMAIL_SMTP_USER`/`EMAIL_SMTP_PASSWORD`/`EMAIL_TO`를 설정하면, 매일(UTC 날짜 기준)
   한 번씩 오늘 실현손익·누적 승률·추세추종 vs 레인지 단타·신뢰도 구간별 성과를
   요약해서 이메일로 보냅니다. **로컬 로그 파일과 달리 이메일은 Render 서버 밖(받는
   사람 메일함)에 남기 때문에, 재배포/슬립으로 `logs/decisions.jsonl`이 초기화돼도
   그 전날까지의 기록은 이메일로 남아있습니다.**
   - Gmail을 쓴다면: `EMAIL_SMTP_HOST=smtp.gmail.com`, `EMAIL_SMTP_PORT=465`,
     `EMAIL_SMTP_USER`에 본인 Gmail 주소, `EMAIL_SMTP_PASSWORD`에는 **일반 비밀번호가
     아니라** [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)
     에서 발급받는 **앱 비밀번호**(2단계 인증 활성화 필요)를 넣으세요.
   - `EMAIL_TO`는 받을 주소(본인 메일 주소로), `EMAIL_FROM`은 비워두면 `EMAIL_SMTP_USER`와
     동일하게 처리됩니다.
   - 필수 값이 하나라도 비어있으면 이 기능은 조용히 꺼진 상태로 유지됩니다(에러 안 남).

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
   `TELEGRAM_CHAT_ID`(선택), `DASHBOARD_TOKEN`(무작위 값으로), `EMAIL_SMTP_HOST`/
   `EMAIL_SMTP_USER`/`EMAIL_SMTP_PASSWORD`/`EMAIL_FROM`/`EMAIL_TO`(선택, 일일 요약
   이메일용 — 아래 "24시간 운영" 섹션 참고).
3. `BYBIT_TESTNET`은 기본 `true`. 실거래로 전환하려면 `false`로 바꾸세요.
4. Start Command는 `gunicorn render_app:app --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 120`
   입니다. **gunicorn 워커는 반드시 1개**여야 합니다 (워커가 늘어나면 봇 루프가 프로세스마다
   중복 실행됩니다).

### B. 수동으로 Web Service 생성

Blueprint를 쓰지 않는다면 New → Web Service로 직접 만들고:
- Build Command: `pip install -r requirements.txt`
- Start Command: `gunicorn render_app:app --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 120`
- 위 3.의 환경변수들을 동일하게 설정.

### C. 플랜 선택: Free + UptimeRobot (기본값) vs Starter + 영구 디스크

`render.yaml`은 기본값이 **`plan: free`** 입니다. Free Web Service는 15분간 아무 HTTP
요청도 없으면 슬립되고, 슬립되는 순간 봇 루프도 같이 멈춥니다. 이를 막으려면
[UptimeRobot](https://uptimerobot.com) 같은 외부 핑 서비스로 슬립되기 전에 주기적으로
요청을 보내야 합니다 (아래 E 참고). 두 방식의 트레이드오프:

| | **Free + UptimeRobot** | **Starter + 영구 디스크** |
|---|---|---|
| 비용 | 무료 | 최소 $7/월 + 디스크 비용(1GB 기준 약 $0.25/월) |
| 슬립 방지 | 핑이 15분 내에 계속 도착해야 함 (실패하면 슬립 → 재기동) | 애초에 슬립 안 함 |
| 상태 영속성 | **없음.** 슬립되거나(핑 실패/지연), 재배포되거나, 크래시 나면 `data/state.json`·`logs/`가 초기화됨 | 영구 디스크 사용 시 재배포/재시작에도 유지됨 |
| 월 사용량 한도 | Render 계정 전체에서 무료 인스턴스 750시간/월 공유 (24시간 서비스 하나만 있어도 거의 꽉 참) | 한도 없음 |

**Free + UptimeRobot을 선택했다면** (지금 `render.yaml` 기본값) 다음을 감안하세요:
- 실제 포지션의 SL/TP는 항상 Bybit 거래소 서버에 주문으로 걸려 있으므로, 봇 프로세스가
  멈춰도(슬립/재기동 중) 청산 자체는 거래소가 대신 집행합니다.
- 하지만 **"일일 손실 한도 도달 시 신규 진입 중단"** 로직은 `data/state.json`의
  `daily.realized_pnl`을 기준으로 판단하는데, 슬립 후 재기동되면 이 값이 사라져 한도가
  리셋됩니다. 즉 한도 보호가 완벽하지 않을 수 있습니다.
- 코드를 자주 배포하는 동안은 `logs/decisions.jsonl`(성과 리뷰용 진입/청산 기록)도 매번
  초기화되므로, 대시보드의 "성과 리뷰(예측 정확도)" 카드로 승률을 분석하려면 **한동안
  재배포 없이 안정적으로 운영**해야 데이터가 쌓입니다.
- 실거래 규모를 키우거나 승률 분석용 데이터를 안정적으로 쌓고 싶다면 Starter +
  영구 디스크로 전환하는 걸 고려해보세요. 전환하려면 `render.yaml`에서 `plan: free`를
  `plan: starter`로 바꾸고 아래를 추가하면 됩니다:
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
6. **(선택) 핑 서비스 이중화**: UptimeRobot 자체가 다운되거나 지연되면 그동안 슬립을
   못 막습니다. [cron-job.org](https://cron-job.org)처럼 다른 무료 핑 서비스에도
   같은 `/healthz` URL을 5~10분 간격으로 등록해두면, 한쪽이 문제 생겨도 다른 쪽이
   계속 깨워줘서 슬립 위험을 줄일 수 있습니다.

---

## 설정 커스터마이즈 (`config.yaml`)

| 항목 | 의미 |
|---|---|
| `exchange.symbols` | 항상 고정으로 감시할 종목 목록 (동적 유니버스와 무관하게 항상 포함) |
| `exchange.universe.*` | 동적 종목 스캔 (`enabled`, `top_n`, `rescan_interval_hours`, `max_taker_fee_rate`) — 켜져 있으면 Bybit 전종목을 24시간 거래대금 기준으로 스캔해서(수수료율 높은 종목은 제외) 상위 `top_n`개를 `exchange.symbols`와 합쳐 감시 |
| `risk.position_size_pct_of_equity` | 포지션당 증거금으로 쓸 자산 비율 (여기에 레버리지를 곱한 게 명목 포지션 크기) |
| `risk.margin_buffer_pct` | 항상 비워둘 증거금 비율. 다른 포지션이 이미 많이 썼으면 새 진입 증거금을 이만큼 남기고 깎음 |
| `risk.max_leverage` | `default_leverage_range`가 아예 없을 때만 쓰이는 최종 폴백 상한 |
| `risk.leverage_by_symbol` | 개별 지정한 종목별 `{min, max}` 레버리지 범위. 신뢰도에 따라 그 범위 내에서 보간 |
| `risk.default_leverage_range` | `leverage_by_symbol`에 없는 모든 종목(동적 유니버스로 들어온 코인 포함)의 `{min, max}` 레버리지 범위 |
| `risk.max_concurrent_positions` | 동시에 보유 가능한 포지션(종목) 최대 개수 |
| `risk.override_entry.*` | 슬롯/증거금이 꽉 찼을 때, 아주 강한 새 신호가 오면 가장 약한 기존 포지션을 청산하고 교체 진입할지 (`enabled`, `min_confidence`, `min_confidence_margin_over_weakest`) |
| `risk.max_daily_loss_pct` | 이 손실률에 도달하면 당일 신규 진입 중단 |
| `risk.min_confidence_to_enter` | 이 신뢰도 미만이면 진입 안 함 (높일수록 거래 빈도↓ 확신도↑) |
| `signals.weights` | 기술적/거래량/뉴스/폴리마켓 각 신호의 반영 비중 |
| `signals.technical.range_lookback` | 레인지 고점/저점을 구할 때 볼 최근 봉 개수 |
| `trade_management.*` | 손절/익절/트레일링/TP연장/긴급탈출 세부 파라미터 |
| `trade_management.stale_exit_after_min` / `stale_exit_after_min_no_pressure` / `stale_exit_max_move_pct` | 이 시간 이상 열려있는데 가격이 이 %만큼도 안 움직였으면(횡보) 손익 무관 정리. 슬롯/증거금 여유가 없을 때는 전자(짧음), 여유 있을 때는 후자(김) 기준 사용. 0으로 끄기 가능 |
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
