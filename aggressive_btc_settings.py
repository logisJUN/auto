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
#
# Raised to 0.3 from 0.2 on 2026-10-20 after live win rate kept declining
# (36.7% -> 29% -> 17% across successive days) as weaker signals were
# traded more often. Lowered back down to 0.25 then 0.22 on 2026-10-21
# (manual edits) -- trade frequency at 0.3 was apparently too slow for the
# user's taste. Current value trades more often on weaker signals than 0.3
# did; see the dashboard's /backtest threshold comparison before moving it
# further in either direction.
ENTRY_THRESHOLD = 0.22
