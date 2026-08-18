"""Funding-rate contrarian signal: perpetual futures periodically exchange a
payment between longs and shorts to keep the perp price anchored to spot.
Extreme positive funding means longs are paying heavily to stay long -- a
crowded, over-leveraged trade prone to long squeezes/pullbacks -- and extreme
negative funding means the same for shorts. Scored *opposite* to the crowd
(positive funding -> bearish score) rather than with it, since the goal is to
avoid piling into an already-crowded trade, not follow it.

Confidence rises with how extreme the rate is (near-zero funding carries no
real positioning information, so it should barely influence the aggregate)
and is zero when the rate couldn't be fetched at all, degrading like every
other signal source on failure instead of blocking the whole aggregate.
"""
from __future__ import annotations


def score(funding_rate: float | None, cfg: dict) -> dict:
    if funding_rate is None:
        return {"score": 0.0, "confidence": 0.0, "funding_rate": None}

    normalize = cfg.get("normalize_pct", 0.05) / 100.0
    if normalize <= 0:
        return {"score": 0.0, "confidence": 0.0, "funding_rate": funding_rate}

    ratio = funding_rate / normalize
    contrarian_score = max(-1.0, min(1.0, -ratio))
    confidence = max(0.0, min(1.0, abs(ratio)))
    return {"score": contrarian_score, "confidence": confidence, "funding_rate": funding_rate}
