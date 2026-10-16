"""Knobs meant to be edited directly, without touching aggressive_btc.py's
strategy logic. Currently just the one: ENTRY_THRESHOLD.
"""

# |blended score| needed to enter a position, in either direction, for every
# configured aggressive_btc symbol (SOXLUSDT + all 38 stock tokens) -- change
# this one number to retune entry sensitivity across all 39 at once.
# Lower = trades more often on weaker signals, so win rate/trade quality can
# drop. Higher = trades less often but only on stronger ones.
#
# Was raised to 0.4 from an original 0.5 for SOXLUSDT specifically because at
# 0.5, 1h SOXLUSDT rarely produced a strong enough blended score to enter at
# all (observed live: a +0.34 blend sat under the bar for hours).
#
# Before changing it, backtest the new value via the dashboard's /backtest
# page (its own entry_threshold field there is independent of this one -- it
# lets you try a value against real history without touching live trading).
ENTRY_THRESHOLD = 0.2
