"""Worst-case scenario sizing: if every configured symbol opened a position at
its max leverage and target margin %, and all of them hit their stop-loss at
the same time, how much of account equity would that cost? Uses live ATR (the
same basis the bot's own SL distance is computed from) rather than a guessed
volatility assumption, so the number reflects current market conditions.

This never places or affects any order -- it's read-only risk reporting for
the dashboard (and can be called from anywhere else that has a BybitClient).
"""
from __future__ import annotations

from bot.signals import technical


def compute_worst_case(client, cfg, equity: float, symbols: list[str] | None = None) -> dict:
    """`symbols` defaults to exchange.symbols (the pinned list) if not given --
    pass the caller's own list (e.g. pinned + currently-open) when the tradable
    universe is dynamic, since testing all ~30 scanned symbols here would mean
    a kline fetch per symbol just for a dashboard card.
    """
    risk_cfg = cfg.get("risk", default={})
    trade_cfg = cfg.get("trade_management", default={})
    tech_cfg = cfg.get("signals", "technical", default={})
    if symbols is None:
        symbols = cfg.get("exchange", "symbols", default=[])

    exec_tf = tech_cfg.get("timeframes", ["15", "60", "240"])[0]
    atr_period = tech_cfg.get("atr_period", 14)
    kline_limit = tech_cfg.get("kline_limit", 200)
    margin_pct = risk_cfg.get("position_size_pct_of_equity", 25.0) / 100.0
    sl_mult = trade_cfg.get("atr_sl_multiplier", 1.5)
    default_lev_range = risk_cfg.get("default_leverage_range", {"min": 1, "max": risk_cfg.get("max_leverage", 5)})

    per_symbol = []
    total_loss_pct = 0.0
    for symbol in symbols:
        lev_range = risk_cfg.get("leverage_by_symbol", {}).get(symbol, default_lev_range)
        leverage = lev_range.get("max", risk_cfg.get("max_leverage", 5))

        try:
            candles = client.get_klines(symbol, exec_tf, kline_limit)
            df = technical.to_dataframe(candles)
            atr_val = float(technical.atr(df, atr_period).iloc[-1])
            close = float(df["close"].iloc[-1])
        except Exception as exc:
            per_symbol.append({"symbol": symbol, "error": str(exc)})
            continue

        if close <= 0 or atr_val <= 0:
            per_symbol.append({"symbol": symbol, "error": "no usable price/ATR data"})
            continue

        sl_distance_pct = (atr_val * sl_mult) / close * 100.0
        loss_pct_of_equity = margin_pct * leverage * sl_distance_pct
        total_loss_pct += loss_pct_of_equity
        per_symbol.append({
            "symbol": symbol,
            "leverage": leverage,
            "sl_distance_pct": sl_distance_pct,
            "loss_pct_of_equity": loss_pct_of_equity,
        })

    max_daily_loss_pct = risk_cfg.get("max_daily_loss_pct", 8.0)
    return {
        "margin_pct_of_equity_per_position": margin_pct * 100.0,
        "per_symbol": per_symbol,
        "worst_case_total_loss_pct": total_loss_pct,
        "worst_case_total_loss_usdt": total_loss_pct / 100.0 * equity,
        "max_daily_loss_pct": max_daily_loss_pct,
        "exceeds_daily_loss_limit": total_loss_pct > max_daily_loss_pct,
    }
