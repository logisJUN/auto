"""Minimal read-only monitoring dashboard, meant to be opened from Safari on an
iPad. Runs as a separate process from the bot (deploy/dashboard.service) so a
dashboard problem can never affect trading. Auto-refreshes every 20s; requires
?token=... matching DASHBOARD_TOKEN in .env.

Run: python -m dashboard.app
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from flask import Flask, abort, render_template_string, request

from bot.config import load_config
from bot.exchange.bybit_client import BybitClient, BybitAPIError
from bot.logger import compute_performance_summary, read_recent_decisions
from bot.risk import stress_test
from bot.state import StateStore

app = Flask(__name__)
cfg = load_config()
client = BybitClient(
    api_key=cfg.secrets.bybit_api_key,
    api_secret=cfg.secrets.bybit_api_secret,
    testnet=cfg.secrets.bybit_testnet,
    category=cfg.get("exchange", "category", default="linear"),
)

# The stress test needs a kline fetch per symbol, and the dashboard auto-refreshes
# every 20s -- cache it for a few minutes so viewing the dashboard doesn't hammer
# Bybit's API on every refresh.
_STRESS_CACHE_TTL_SEC = 300
_stress_cache = {"ts": 0.0, "data": None}


def _build_watchlist(client: BybitClient, symbols: list[str], trades: dict) -> list[dict]:
    """Always-visible status for the pinned symbols (BTCUSDT/ETHUSDT), regardless
    of whether a position is currently open -- unlike the "open positions" table
    below, which only lists symbols with a live trade.
    """
    out = []
    for symbol in symbols:
        try:
            price = client.get_last_price(symbol)
        except BybitAPIError:
            price = None
        trade = trades.get(symbol)
        if trade is None:
            out.append({"symbol": symbol, "price": price, "status": "flat"})
            continue
        if price is not None:
            unrealized = (price - trade["entry_price"]) * trade["qty"] if trade["side"] == "long" \
                else (trade["entry_price"] - price) * trade["qty"]
        else:
            unrealized = None
        out.append({
            "symbol": symbol, "price": price, "status": "open",
            "side": trade["side"], "entry_price": trade["entry_price"], "unrealized": unrealized,
        })
    return out


def _build_equity_curve(history: list[dict]) -> dict | None:
    """SVG polyline of cumulative realized PnL over time, in chronological order,
    from state.history (closed-trade summaries). Not a full tick-by-tick equity
    curve (unrealized swings between closes aren't captured, and this resets
    whenever state.json is reset -- e.g. a Render free-plan disk wipe), but it's
    the only "account trend over time" data actually persisted, and needs no new
    tracking to show.
    """
    ordered = sorted(history, key=lambda h: h.get("closed_at", 0.0))
    if len(ordered) < 2:
        return None

    points = []
    cum = 0.0
    for h in ordered:
        cum += h.get("pnl") or 0.0
        points.append((h.get("closed_at", 0.0), cum))

    xs = [p[0] for p in points]
    ys = [p[1] for p in points] + [0.0]  # always include the zero baseline in range
    x_min, x_max = min(xs), max(xs)
    y_min, y_max = min(ys), max(ys)
    x_range = (x_max - x_min) or 1.0
    y_range = (y_max - y_min) or 1.0

    width, height, pad = 600, 160, 10

    def sx(x):
        return pad + (x - x_min) / x_range * (width - 2 * pad)

    def sy(y):
        return height - pad - (y - y_min) / y_range * (height - 2 * pad)

    svg_points = " ".join(f"{sx(x):.1f},{sy(y):.1f}" for x, y in points)
    last_cum = points[-1][1]
    return {
        "svg_points": svg_points, "width": width, "height": height,
        "zero_y": round(sy(0.0), 1), "min_pnl": y_min, "max_pnl": y_max,
        "last_cum": last_cum, "point_count": len(points),
    }


def _get_stress_test(equity: float, open_symbols: list[str]) -> dict | None:
    now = time.time()
    if _stress_cache["data"] is None or now - _stress_cache["ts"] > _STRESS_CACHE_TTL_SEC:
        # With the dynamic universe scan on, exchange.symbols is just the pinned
        # list -- test that plus whatever's actually open, not all ~30 scanned
        # symbols (that would mean a kline fetch per symbol on every cache miss).
        pinned = cfg.get("exchange", "symbols", default=[])
        symbols = list(dict.fromkeys(list(pinned) + list(open_symbols)))
        try:
            _stress_cache["data"] = stress_test.compute_worst_case(client, cfg, equity, symbols=symbols)
            _stress_cache["ts"] = now
        except Exception:
            return _stress_cache["data"]
    return _stress_cache["data"]

TEMPLATE = """
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="20">
<title>Bybit Bot Dashboard</title>
<style>
  body { font-family: -apple-system, system-ui, sans-serif; background:#0d1117; color:#e6edf3; margin:0; padding:16px; }
  h1 { font-size:1.2rem; margin-bottom:4px; }
  .mode { display:inline-block; padding:2px 8px; border-radius:6px; font-size:0.8rem; margin-bottom:12px; }
  .mode.testnet { background:#264d1a; color:#7ee787; }
  .mode.live { background:#4d1a1a; color:#ff7b72; }
  .card { background:#161b22; border:1px solid #30363d; border-radius:10px; padding:12px; margin-bottom:14px; }
  .grid { display:grid; grid-template-columns: repeat(auto-fit, minmax(140px,1fr)); gap:8px; }
  .stat-label { font-size:0.75rem; color:#8b949e; }
  .stat-value { font-size:1.1rem; font-weight:600; }
  .pos-long { color:#7ee787; } .pos-short { color:#ff7b72; }
  table { width:100%; border-collapse:collapse; font-size:0.85rem; }
  th, td { text-align:left; padding:4px 6px; border-bottom:1px solid #21262d; }
  .pnl-pos { color:#7ee787; } .pnl-neg { color:#ff7b72; }
  .small { color:#8b949e; font-size:0.75rem; }
</style>
</head>
<body>
  <h1>Bybit Futures Bot</h1>
  <span class="mode {{ 'testnet' if testnet else 'live' }}">{{ 'TESTNET (모의투자)' if testnet else 'LIVE (실거래)' }}</span>

  <div class="card">
    <div class="grid">
      <div><div class="stat-label">계좌 자산 (USDT)</div><div class="stat-value">{{ equity }}</div></div>
      <div><div class="stat-label">총 미실현손익</div><div class="stat-value {{ 'pnl-pos' if total_unrealized >= 0 else 'pnl-neg' }}">{{ '%.4f'|format(total_unrealized) }}</div></div>
      <div><div class="stat-label">오늘 실현 손익</div><div class="stat-value {{ 'pnl-pos' if daily_pnl >= 0 else 'pnl-neg' }}">{{ '%.4f'|format(daily_pnl) }}</div></div>
      <div><div class="stat-label">오늘 손실률 / 한도</div><div class="stat-value">{{ '%.2f'|format(daily_loss_pct) }}% / {{ max_daily_loss_pct }}%</div></div>
      <div><div class="stat-label">보유 포지션</div><div class="stat-value">{{ open_count }} / {{ max_positions }}</div></div>
    </div>
  </div>

  <div class="card">
    <h2 style="font-size:1rem;">감시 종목</h2>
    <div class="grid">
      {% for w in watchlist %}
      <div>
        <div class="stat-label">{{ w.symbol }}</div>
        <div class="stat-value">{{ '%.4f'|format(w.price) if w.price is not none else '조회 실패' }}</div>
        {% if w.status == 'open' %}
        <div class="small {{ 'pos-long' if w.side == 'long' else 'pos-short' }}">
          {{ w.side.upper() }} 진입 {{ w.entry_price }}
          {% if w.unrealized is not none %} / 미실현 <span class="{{ 'pnl-pos' if w.unrealized >= 0 else 'pnl-neg' }}">{{ '%.4f'|format(w.unrealized) }}</span>{% endif %}
        </div>
        {% else %}
        <div class="small">관망 중 - 진입 신호 대기</div>
        {% endif %}
      </div>
      {% endfor %}
    </div>
  </div>

  <div class="card">
    <h2 style="font-size:1rem;">열린 포지션</h2>
    {% if positions %}
    <table>
      <tr><th>심볼</th><th>방향</th><th>수량</th><th>진입가</th><th>현재가</th><th>SL</th><th>TP</th><th>미실현손익</th></tr>
      {% for p in positions %}
      <tr>
        <td>{{ p.symbol }}</td>
        <td class="{{ 'pos-long' if p.side == 'long' else 'pos-short' }}">{{ p.side.upper() }}</td>
        <td>{{ p.qty }}</td>
        <td>{{ p.entry_price }}</td>
        <td>{{ p.last_price }}</td>
        <td>{{ p.current_sl }}</td>
        <td>{{ p.current_tp }}</td>
        <td class="{{ 'pnl-pos' if p.unrealized >= 0 else 'pnl-neg' }}">{{ '%.4f'|format(p.unrealized) }}</td>
      </tr>
      {% endfor %}
    </table>
    {% else %}
    <div class="small">현재 열린 포지션 없음 - 진입 신호 대기 중</div>
    {% endif %}
  </div>

  {% if equity_curve %}
  <div class="card">
    <h2 style="font-size:1rem;">손익 변동 추이 (누적 실현손익)</h2>
    <svg viewBox="0 0 {{ equity_curve.width }} {{ equity_curve.height }}" style="width:100%; height:auto;">
      <line x1="0" y1="{{ equity_curve.zero_y }}" x2="{{ equity_curve.width }}" y2="{{ equity_curve.zero_y }}"
            stroke="#30363d" stroke-width="1" stroke-dasharray="4 3" />
      <polyline points="{{ equity_curve.svg_points }}" fill="none"
                stroke="{{ '#7ee787' if equity_curve.last_cum >= 0 else '#ff7b72' }}" stroke-width="2" />
    </svg>
    <div class="grid" style="margin-top:4px;">
      <div><div class="stat-label">누적 실현손익</div><div class="stat-value {{ 'pnl-pos' if equity_curve.last_cum >= 0 else 'pnl-neg' }}">{{ '%.4f'|format(equity_curve.last_cum) }}</div></div>
      <div><div class="stat-label">최고</div><div class="stat-value pnl-pos">{{ '%.4f'|format(equity_curve.max_pnl) }}</div></div>
      <div><div class="stat-label">최저</div><div class="stat-value pnl-neg">{{ '%.4f'|format(equity_curve.min_pnl) }}</div></div>
    </div>
    <div class="small">청산된 거래 {{ equity_curve.point_count }}건 기준 (미실현 손익 변동은 반영 안 됨). 상태 초기화 시 리셋됩니다.</div>
  </div>
  {% endif %}

  <div class="card">
    <h2 style="font-size:1rem;">거래 이력</h2>
    {% if trade_history %}
    <table>
      <tr><th>시각</th><th>심볼</th><th>방향</th><th>진입가</th><th>청산가</th><th>수량</th><th>손익</th><th>사유</th></tr>
      {% for t in trade_history %}
      <tr>
        <td class="small">{{ t.time_str }}</td>
        <td>{{ t.symbol }}</td>
        <td class="{{ 'pos-long' if t.side == 'long' else 'pos-short' }}">{{ t.side.upper() }}</td>
        <td>{{ t.entry_price }}</td>
        <td>{{ t.exit_price }}</td>
        <td>{{ t.qty }}</td>
        <td class="{{ 'pnl-pos' if t.pnl >= 0 else 'pnl-neg' }}">{{ '%.4f'|format(t.pnl) }}{{ ' (est.)' if t.pnl_is_estimate else '' }}</td>
        <td class="small">{{ t.reason }}</td>
      </tr>
      {% endfor %}
    </table>
    {% else %}
    <div class="small">아직 청산된 거래가 없습니다.</div>
    {% endif %}
  </div>

  {% if stress %}
  <div class="card">
    <h2 style="font-size:1rem;">리스크 스트레스 테스트 (전종목 동시 손절 가정)</h2>
    <table>
      <tr><th>심볼</th><th>레버리지</th><th>손절폭</th><th>손실(자산 대비)</th></tr>
      {% for s in stress.per_symbol %}
      <tr>
        <td>{{ s.symbol }}</td>
        {% if s.error %}
        <td colspan="3" class="small">조회 실패: {{ s.error }}</td>
        {% else %}
        <td>{{ s.leverage }}x</td>
        <td>{{ '%.2f'|format(s.sl_distance_pct) }}%</td>
        <td>{{ '%.2f'|format(s.loss_pct_of_equity) }}%</td>
        {% endif %}
      </tr>
      {% endfor %}
    </table>
    <div style="margin-top:8px;" class="{{ 'pnl-neg' if stress.exceeds_daily_loss_limit else 'pnl-pos' }}">
      합계: 자산의 {{ '%.2f'|format(stress.worst_case_total_loss_pct) }}%
      (~{{ '%.2f'|format(stress.worst_case_total_loss_usdt) }} USDT)
      / 일일 손실 한도 {{ stress.max_daily_loss_pct }}%
      {% if stress.exceeds_daily_loss_limit %} — 한도 초과 가능{% endif %}
    </div>
    <div class="small">전종목이 목표 증거금%·최대 레버리지로 동시에 진입해 있다가 전부 손절될 경우를 가정한 수치입니다 (실제 마진 버퍼 정책으로 실제 배분은 이보다 작을 수 있음). 5분마다 갱신됩니다.</div>
  </div>
  {% endif %}

  {% if perf.overall.count > 0 %}
  <div class="card">
    <h2 style="font-size:1rem;">성과 리뷰 (예측 정확도)</h2>
    <div class="grid">
      <div><div class="stat-label">전체 거래</div><div class="stat-value">{{ perf.overall.count }}건</div></div>
      <div><div class="stat-label">승률</div><div class="stat-value">{{ '%.0f'|format(perf.overall.win_rate * 100) }}%</div></div>
      <div><div class="stat-label">누적 손익</div><div class="stat-value {{ 'pnl-pos' if perf.overall.total_pnl >= 0 else 'pnl-neg' }}">{{ '%.4f'|format(perf.overall.total_pnl) }}</div></div>
    </div>
    <table style="margin-top:8px;">
      <tr><th>구분</th><th>거래수</th><th>승률</th><th>누적손익</th></tr>
      <tr>
        <td>추세추종</td><td>{{ perf.by_type.trend.count }}</td>
        <td>{{ '%.0f'|format(perf.by_type.trend.win_rate * 100) if perf.by_type.trend.win_rate is not none else '-' }}{{ '%' if perf.by_type.trend.win_rate is not none }}</td>
        <td>{{ '%.4f'|format(perf.by_type.trend.total_pnl) }}</td>
      </tr>
      <tr>
        <td>레인지 단타</td><td>{{ perf.by_type.range.count }}</td>
        <td>{{ '%.0f'|format(perf.by_type.range.win_rate * 100) if perf.by_type.range.win_rate is not none else '-' }}{{ '%' if perf.by_type.range.win_rate is not none }}</td>
        <td>{{ '%.4f'|format(perf.by_type.range.total_pnl) }}</td>
      </tr>
      {% for label, s in perf.by_confidence.items() %}
      <tr>
        <td>신뢰도 {{ label }}</td><td>{{ s.count }}</td>
        <td>{{ '%.0f'|format(s.win_rate * 100) }}%</td>
        <td>{{ '%.4f'|format(s.total_pnl) }}</td>
      </tr>
      {% endfor %}
    </table>
    <div class="small">decisions.jsonl의 진입/청산 기록을 짝지어 계산합니다 (재배포로 로그가 초기화되면 리셋됩니다).</div>
  </div>
  {% endif %}

  <div class="card">
    <h2 style="font-size:1rem;">최근 판단 로그</h2>
    <table>
      <tr><th>시각</th><th>이벤트</th><th>내용</th></tr>
      {% for d in decisions %}
      <tr>
        <td class="small">{{ d.time_str }}</td>
        <td>{{ d.event }}</td>
        <td class="small">{{ d.summary }}</td>
      </tr>
      {% endfor %}
    </table>
  </div>

  <div class="small">20초마다 자동 새로고침 됩니다.</div>
</body>
</html>
"""


def _check_token():
    token = request.args.get("token", "")
    if token != cfg.secrets.dashboard_token:
        abort(403)


def _decision_summary(d: dict) -> str:
    event = d.get("event")
    if event == "entry":
        return f"{d.get('symbol')} {d.get('side','').upper()} entry={d.get('entry_price')} SL={d.get('sl')} TP={d.get('tp')}"
    if event == "exit":
        return f"{d.get('symbol')} reason={d.get('reason')} pnl={d.get('pnl'):.4f}" if d.get('pnl') is not None else str(d)
    if event == "adjust":
        return f"{d.get('symbol')} SL={d.get('sl')} TP={d.get('tp')} ({d.get('reason')})"
    return str({k: v for k, v in d.items() if k not in ("ts", "signal")})


@app.route("/healthz")
def healthz():
    # Public, no token, no Bybit calls -- meant for uptime pingers (e.g.
    # UptimeRobot) keeping a Render free-plan service from sleeping.
    return "ok", 200


@app.route("/")
def index():
    _check_token()
    state = StateStore(os.path.join(os.getenv("DATA_DIR", "data"), "state.json"))
    snapshot = state.snapshot()

    try:
        equity = client.get_equity_usdt()
    except BybitAPIError:
        equity = snapshot.get("daily", {}).get("start_equity") or 0.0

    daily = snapshot.get("daily", {})
    daily_pnl = daily.get("realized_pnl", 0.0)
    daily_loss_pct = state.daily_loss_pct(equity)

    positions = []
    for symbol, trade in snapshot.get("trades", {}).items():
        try:
            last_price = client.get_last_price(symbol)
        except BybitAPIError:
            last_price = trade["entry_price"]
        if trade["side"] == "long":
            unrealized = (last_price - trade["entry_price"]) * trade["qty"]
        else:
            unrealized = (trade["entry_price"] - last_price) * trade["qty"]
        positions.append({**trade, "last_price": round(last_price, 6), "unrealized": unrealized})

    total_unrealized = sum(p["unrealized"] for p in positions)

    log_dir = os.getenv("LOG_DIR", "logs")
    decisions_raw = read_recent_decisions(log_dir, limit=30)
    decisions = []
    for d in decisions_raw:
        decisions.append({
            "time_str": time.strftime("%m-%d %H:%M:%S", time.localtime(d.get("ts", time.time()))),
            "event": d.get("event", "?"),
            "summary": _decision_summary(d),
        })

    watched_symbols = cfg.get("exchange", "symbols", default=[])
    watchlist = _build_watchlist(client, watched_symbols, snapshot.get("trades", {}))

    trade_history = []
    for t in sorted(snapshot.get("history", []), key=lambda h: h.get("closed_at", 0.0), reverse=True)[:50]:
        trade_history.append({
            **t,
            "time_str": time.strftime("%m-%d %H:%M:%S", time.localtime(t.get("closed_at", time.time()))),
        })
    equity_curve = _build_equity_curve(snapshot.get("history", []))

    stress = _get_stress_test(equity, list(snapshot.get("trades", {}).keys())) if equity > 0 else None
    perf = compute_performance_summary(log_dir)

    return render_template_string(
        TEMPLATE,
        testnet=cfg.secrets.bybit_testnet,
        equity=round(equity, 4),
        total_unrealized=total_unrealized,
        daily_pnl=daily_pnl,
        daily_loss_pct=daily_loss_pct,
        max_daily_loss_pct=cfg.get("risk", "max_daily_loss_pct", default=8.0),
        open_count=len(positions),
        max_positions=cfg.get("risk", "max_concurrent_positions", default=1),
        positions=positions,
        watchlist=watchlist,
        trade_history=trade_history,
        equity_curve=equity_curve,
        decisions=decisions,
        stress=stress,
        perf=perf,
    )


def main():
    host = cfg.get("dashboard", "host", default="0.0.0.0")
    # Render (and most PaaS) assign the listen port via $PORT at runtime.
    port = int(os.getenv("PORT", cfg.get("dashboard", "port", default=8080)))
    app.run(host=host, port=port)


if __name__ == "__main__":
    main()
