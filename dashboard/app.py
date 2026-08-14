"""Mostly read-only monitoring dashboard, meant to be opened from Safari on an
iPad. Runs as a separate process from the bot (deploy/dashboard.service) so a
dashboard problem can never affect trading -- the one exception is the daily-
loss reset button, which writes a tiny sentinel file the bot's own process
polls for and acts on itself (see StateStore.request_daily_reset), rather
than mutating shared state directly. Auto-refreshes every 20s; requires
?token=... matching DASHBOARD_TOKEN in .env.

Run: python -m dashboard.app
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import requests
from flask import Flask, abort, redirect, render_template_string, request

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

# USD/KRW moves slowly enough that an hourly refresh is plenty -- avoids a
# forex API call on every 20s dashboard refresh. Free, no-key endpoint; if it
# ever fails, the dashboard just omits the KRW figure rather than erroring.
_FX_CACHE_TTL_SEC = 3600
_fx_cache = {"ts": 0.0, "usd_krw": None}


def _get_usd_krw_rate() -> float | None:
    now = time.time()
    if now - _fx_cache["ts"] < _FX_CACHE_TTL_SEC and _fx_cache["usd_krw"] is not None:
        return _fx_cache["usd_krw"]
    try:
        resp = requests.get("https://open.er-api.com/v6/latest/USD", timeout=5)
        resp.raise_for_status()
        rate = float(resp.json()["rates"]["KRW"])
    except Exception:
        return _fx_cache["usd_krw"]  # keep serving the last good rate if we have one
    _fx_cache["usd_krw"] = rate
    _fx_cache["ts"] = now
    return rate


def _build_watchlist(client: BybitClient, symbols: list[str], trades: dict, skip_reasons: dict | None = None) -> list[dict]:
    """Always-visible status for the pinned symbols (BTCUSDT/ETHUSDT), regardless
    of whether a position is currently open -- unlike the "open positions" table
    below, which only lists symbols with a live trade.

    `skip_reasons` surfaces WHY try_enter() last passed on this symbol (recorded
    live in Strategy.try_enter/_open/_enter_range) instead of the dashboard only
    ever showing a generic "waiting for a signal" -- answers "지금 왜 거래가 없어?"
    directly instead of needing a guess at which of several possible gates (daily
    loss cap, confidence threshold, cooldown, no margin room, ...) is active.
    """
    skip_reasons = skip_reasons or {}
    out = []
    for symbol in symbols:
        try:
            price = client.get_last_price(symbol)
        except BybitAPIError:
            price = None
        trade = trades.get(symbol)
        if trade is None:
            skip = skip_reasons.get(symbol)
            out.append({"symbol": symbol, "price": price, "status": "flat",
                        "skip_reason": skip.get("reason") if skip else None})
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


def _get_stress_test(equity: float, open_trades: dict) -> dict | None:
    # With the dynamic universe scan on, exchange.symbols is just the pinned
    # list -- test that plus whatever's actually open, not all ~30 scanned
    # symbols (that would mean a kline fetch per symbol on every cache miss).
    pinned = cfg.get("exchange", "symbols", default=[])
    symbols = list(dict.fromkeys(list(pinned) + list(open_trades.keys())))
    symbols_key = tuple(sorted(symbols))

    now = time.time()
    # Refresh immediately if the set of open/pinned symbols changed (a newly
    # opened position on a universe-scanned symbol must show up right away,
    # not sit invisible to the risk report for up to 5 minutes), otherwise
    # only on the normal TTL -- avoids a kline fetch per symbol on every 20s
    # dashboard refresh for an unchanged set of positions.
    stale = (_stress_cache["data"] is None
             or now - _stress_cache["ts"] > _STRESS_CACHE_TTL_SEC
             or _stress_cache.get("symbols_key") != symbols_key)
    if stale:
        try:
            _stress_cache["data"] = stress_test.compute_worst_case(
                client, cfg, equity, symbols=symbols, open_trades=open_trades)
            _stress_cache["ts"] = now
            _stress_cache["symbols_key"] = symbols_key
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
  .copy-btn { float:right; background:#21262d; color:#e6edf3; border:1px solid #30363d;
              border-radius:6px; padding:6px 12px; font-size:0.85rem; }
  .copy-btn:active { background:#30363d; }
  .reset-btn { width:100%; background:#4d1a1a; color:#ff7b72; border:1px solid #6e2323;
               border-radius:6px; padding:10px 12px; font-size:0.85rem; font-weight:600; }
  .reset-btn:active { background:#6e2323; }
</style>
</head>
<body>
  <button id="copy-btn" class="copy-btn" onclick="copyReview()">📋 리뷰 복사</button>
  <div id="report">
  <h1>Bybit Futures Bot</h1>
  <span class="mode {{ 'testnet' if testnet else 'live' }}">{{ 'TESTNET (모의투자)' if testnet else 'LIVE (실거래)' }}</span>
  <div class="card">
    <div class="grid">
      <div><div class="stat-label">계좌 자산 (USDT)</div><div class="stat-value">{{ equity }}</div>
        {% if equity_krw is not none %}<div class="small">≈ {{ '{:,.0f}'.format(equity_krw) }}원</div>{% endif %}
      </div>
      <div><div class="stat-label">총 미실현손익</div><div class="stat-value {{ 'pnl-pos' if total_unrealized >= 0 else 'pnl-neg' }}">{{ '%.4f'|format(total_unrealized) }}</div></div>
      <div><div class="stat-label">오늘 실현 손익</div><div class="stat-value {{ 'pnl-pos' if daily_pnl >= 0 else 'pnl-neg' }}">{{ '%.4f'|format(daily_pnl) }}</div></div>
      <div><div class="stat-label">오늘 손익률 / 한도</div><div class="stat-value {{ 'pnl-pos' if daily_change_pct >= 0 else 'pnl-neg' }}">{{ '%+.2f'|format(daily_change_pct) }}% / {{ max_daily_loss_pct }}%</div></div>
      <div><div class="stat-label">보유 포지션</div><div class="stat-value">{{ open_count }} / {{ max_positions }}</div></div>
    </div>
    {% if daily_change_pct <= -max_daily_loss_pct %}
    <form method="POST" action="/reset-daily-loss?token={{ token }}" style="margin-top:10px;"
          onsubmit="return confirm('오늘 손익 한도를 초기화하고 거래를 재개할까요? 거래 이력은 유지됩니다.');">
      <button type="submit" class="reset-btn">⚠️ 일일 손실 한도 초기화하고 거래 재개</button>
    </form>
    {% endif %}
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
        <div class="small">관망 중{% if w.skip_reason %} - {{ w.skip_reason }}{% else %} - 진입 신호 대기{% endif %}</div>
        {% endif %}
      </div>
      {% endfor %}
    </div>
  </div>

  <div class="card">
    <h2 style="font-size:1rem;">열린 포지션</h2>
    {% if positions %}
    <table>
      <tr><th>심볼</th><th>방향</th><th>레버리지</th><th>수량</th><th>진입가</th><th>현재가</th><th>SL</th><th>TP</th><th>미실현손익</th></tr>
      {% for p in positions %}
      <tr>
        <td>{{ p.symbol }}</td>
        <td class="{{ 'pos-long' if p.side == 'long' else 'pos-short' }}">{{ p.side.upper() }}</td>
        <td>{{ p.leverage }}x ({{ '%.1f'|format(p.margin_pct) }}%)</td>
        <td>{{ p.qty }}</td>
        <td>{{ p.entry_price }}</td>
        <td>{{ p.last_price }}</td>
        <td>{{ p.current_sl }} ({{ '%+.2f'|format(p.sl_pct) }}%)</td>
        <td>{{ p.current_tp }} ({{ '%+.2f'|format(p.tp_pct) }}%)</td>
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
      <tr><th>시각</th><th>심볼</th><th>방향</th><th>진입가</th><th>청산가</th><th>수량</th><th>진입 USDT</th><th>손익</th><th>손익%</th><th>수수료</th><th>사유</th></tr>
      {% for t in trade_history %}
      <tr>
        <td class="small">{{ t.time_str }}</td>
        <td>{{ t.symbol }}</td>
        <td class="{{ 'pos-long' if t.side == 'long' else 'pos-short' }}">{{ t.side.upper() }}</td>
        <td>{{ t.entry_price }}</td>
        <td>{{ t.exit_price }}</td>
        <td>{{ t.qty }}</td>
        <td class="small">{{ '%.2f'|format(t.entry_margin_usdt) if t.entry_margin_usdt is not none else '-' }}</td>
        <td class="{{ 'pnl-pos' if t.pnl >= 0 else 'pnl-neg' }}">{{ '%.4f'|format(t.pnl) }}{{ ' (est.)' if t.pnl_is_estimate else '' }}</td>
        <td class="{{ 'pnl-pos' if t.pnl >= 0 else 'pnl-neg' }}">{{ '%+.2f'|format(t.pnl_pct) + '%' if t.pnl_pct is not none else '-' }}</td>
        <td class="small">{{ '-%.4f'|format(t.fees_paid) if t.fees_paid is defined and t.fees_paid is not none else '-' }}</td>
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
    {% if perf.by_symbol %}
    <h2 style="font-size:1rem; margin-top:12px;">종목별 성과</h2>
    <table>
      <tr><th>심볼</th><th>거래수</th><th>승률</th><th>누적손익</th></tr>
      {% for symbol, s in perf.by_symbol.items() %}
      <tr>
        <td>{{ symbol }}</td><td>{{ s.count }}</td>
        <td>{{ '%.0f'|format(s.win_rate * 100) if s.win_rate is not none else '-' }}{{ '%' if s.win_rate is not none }}</td>
        <td class="{{ 'pnl-pos' if s.total_pnl >= 0 else 'pnl-neg' }}">{{ '%.4f'|format(s.total_pnl) }}</td>
      </tr>
      {% endfor %}
    </table>
    {% endif %}
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
  </div>

  <script>
    function copyReview() {
      const text = document.getElementById('report').innerText;
      const btn = document.getElementById('copy-btn');
      const original = btn.textContent;
      navigator.clipboard.writeText(text).then(() => {
        btn.textContent = '복사됨!';
        setTimeout(() => { btn.textContent = original; }, 1500);
      }).catch(() => {
        btn.textContent = '복사 실패';
        setTimeout(() => { btn.textContent = original; }, 1500);
      });
    }
  </script>
</body>
</html>
"""


def _check_token():
    token = request.args.get("token", "")
    if token != cfg.secrets.dashboard_token:
        abort(403)


_COMPONENT_ABBR = {"technical": "T", "volume": "V", "news": "N", "polymarket": "P", "funding": "F"}


def _component_breakdown(signal: dict) -> str:
    """Compact per-signal-source score breakdown (e.g. 'T:+0.30 V:+0.10
    N:-0.05 P:+0.20 F:-0.10') -- lets you tell which sub-signal is driving (or
    fighting) the final direction, instead of only ever seeing the one
    blended confidence number.
    """
    components = signal.get("components") or {}
    parts = []
    for key, abbr in _COMPONENT_ABBR.items():
        c = components.get(key)
        if c:
            parts.append(f"{abbr}:{c.get('score', 0.0):+.2f}")
    return " ".join(parts)


def _decision_summary(d: dict) -> str:
    event = d.get("event")
    if event == "entry":
        base = f"{d.get('symbol')} {d.get('side','').upper()} entry={d.get('entry_price')} SL={d.get('sl')} TP={d.get('tp')}"
        signal = d.get("signal")
        if signal:
            breakdown = _component_breakdown(signal)
            if breakdown:
                base += f" [{breakdown}]"
        extension = d.get("entry_extension_atr_mult")
        if extension is not None:
            base += f" Ext={extension:+.1f}×ATR"
        return base
    if event == "exit":
        if d.get('pnl') is None:
            return str(d)
        base = f"{d.get('symbol')} reason={d.get('reason')} pnl={d.get('pnl'):.4f}"
        fees = d.get("fees_paid")
        if fees is not None:
            base += f" (수수료 {fees:+.4f})"
        return base
    if event == "adjust":
        return f"{d.get('symbol')} SL={d.get('sl')} TP={d.get('tp')} ({d.get('reason')})"
    return str({k: v for k, v in d.items() if k not in ("ts", "signal")})


@app.route("/healthz")
def healthz():
    # Public, no token, no Bybit calls -- meant for uptime pingers (e.g.
    # UptimeRobot) keeping a Render free-plan service from sleeping.
    return "ok", 200


@app.route("/reset-daily-loss", methods=["POST"])
def reset_daily_loss():
    """Lets today's max_daily_loss_pct stop (and the force-flatten it
    triggers) be lifted on demand instead of waiting for UTC day rollover --
    trade history/decisions.jsonl are untouched, only today's start_equity/
    realized_pnl tracking resets. Doesn't reset state.json directly (see the
    module docstring and StateStore.request_daily_reset for why); just flags
    the bot's own process to do it on its next tick.
    """
    _check_token()
    state = StateStore(os.path.join(os.getenv("DATA_DIR", "data"), "state.json"))
    state.request_daily_reset()
    token = request.args.get("token", "")
    return redirect(f"/?token={token}")


@app.route("/")
def index():
    _check_token()
    state = StateStore(os.path.join(os.getenv("DATA_DIR", "data"), "state.json"))
    snapshot = state.snapshot()

    try:
        equity = client.get_equity_usdt()
    except BybitAPIError:
        equity = snapshot.get("daily", {}).get("start_equity") or 0.0

    usd_krw = _get_usd_krw_rate()
    equity_krw = equity * usd_krw if usd_krw is not None else None

    daily = snapshot.get("daily", {})
    daily_pnl = daily.get("realized_pnl", 0.0)
    daily_loss_pct = state.daily_loss_pct(equity)
    # daily_loss_pct is clamped to >=0 (it's compared against the loss-limit
    # circuit breaker) -- the dashboard wants the real signed change (a gain
    # shows as a real "+", not clamped to 0), so compute that separately here.
    start_equity = daily.get("start_equity") or 0.0
    daily_change_pct = ((equity - start_equity) / start_equity * 100.0) if start_equity > 0 else 0.0

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
        leverage = trade.get("leverage") or 1
        margin_pct = (trade["qty"] * trade["entry_price"] / leverage) / equity * 100.0 if equity > 0 else 0.0
        entry = trade["entry_price"]
        sl_pct = (trade["current_sl"] / entry - 1) * 100.0 if entry > 0 else 0.0
        tp_pct = (trade["current_tp"] / entry - 1) * 100.0 if entry > 0 else 0.0
        positions.append({**trade, "last_price": round(last_price, 6), "unrealized": unrealized,
                           "margin_pct": margin_pct, "sl_pct": sl_pct, "tp_pct": tp_pct})

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
    watchlist = _build_watchlist(client, watched_symbols, snapshot.get("trades", {}), snapshot.get("skip_reasons", {}))

    trade_history = []
    for t in sorted(snapshot.get("history", []), key=lambda h: h.get("closed_at", 0.0), reverse=True)[:50]:
        leverage = t.get("leverage")
        # Margin actually committed at entry (notional / leverage), not the
        # notional itself -- this is what "몇 USDT로 들어갔는지" actually means
        # for a leveraged position, and what pnl_pct (real return on the
        # capital committed) is measured against. None for trades closed
        # before this field existed (leverage wasn't recorded on old rows).
        entry_margin_usdt = (t.get("entry_price", 0.0) * t.get("qty", 0.0) / leverage) if leverage else None
        pnl_pct = (t.get("pnl", 0.0) / entry_margin_usdt * 100.0) if entry_margin_usdt else None
        trade_history.append({
            **t,
            "time_str": time.strftime("%m-%d %H:%M:%S", time.localtime(t.get("closed_at", time.time()))),
            "entry_margin_usdt": entry_margin_usdt,
            "pnl_pct": pnl_pct,
        })
    equity_curve = _build_equity_curve(snapshot.get("history", []))

    stress = _get_stress_test(equity, snapshot.get("trades", {})) if equity > 0 else None
    perf = compute_performance_summary(log_dir)

    return render_template_string(
        TEMPLATE,
        token=request.args.get("token", ""),
        testnet=cfg.secrets.bybit_testnet,
        equity=round(equity, 4),
        equity_krw=equity_krw,
        total_unrealized=total_unrealized,
        daily_pnl=daily_pnl,
        daily_loss_pct=daily_loss_pct,
        daily_change_pct=daily_change_pct,
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
