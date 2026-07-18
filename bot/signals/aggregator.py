"""Combines technical, volume, news and polymarket signals into one decision.

Each sub-signal contributes a score in [-1, 1] (positive = bullish). A signal's
influence on the final result is its configured weight times its own confidence,
so a quiet news day (few articles) or a Polymarket signal with only one relevant
market naturally counts for less than a strong, well-attested technical trend.

Overall confidence blends three things: how strong the final score is, how much
the individual signals agree with each other, and how much real data backed the
signals in the first place. min_confidence_to_enter in config.yaml is compared
against this value before the bot will ever open a trade.
"""
from __future__ import annotations


def aggregate(technical: dict, news: dict, polymarket: dict, weights: dict) -> dict:
    w_tech = weights.get("technical", 0.45)
    w_vol = weights.get("volume", 0.15)
    w_news = weights.get("news", 0.20)
    w_poly = weights.get("polymarket", 0.20)

    components = {
        "technical": {"score": technical["score"], "confidence": 1.0, "weight": w_tech},
        "volume": {"score": technical.get("volume_score", 0.0), "confidence": 1.0, "weight": w_vol},
        "news": {"score": news["score"], "confidence": news["confidence"], "weight": w_news},
        "polymarket": {"score": polymarket["score"], "confidence": polymarket["confidence"], "weight": w_poly},
    }

    eff_weight_sum = 0.0
    weighted_score_sum = 0.0
    weight_budget = 0.0
    data_weighted_confidence = 0.0
    for c in components.values():
        eff_weight = c["weight"] * c["confidence"]
        weighted_score_sum += c["score"] * eff_weight
        eff_weight_sum += eff_weight
        weight_budget += c["weight"]
        data_weighted_confidence += c["weight"] * c["confidence"]

    final_score = weighted_score_sum / eff_weight_sum if eff_weight_sum > 1e-9 else 0.0
    final_score = max(-1.0, min(1.0, final_score))

    if eff_weight_sum > 1e-9:
        agree_weight = sum(
            c["weight"] * c["confidence"]
            for c in components.values()
            if (c["score"] >= 0) == (final_score >= 0)
        )
        agreement = agree_weight / eff_weight_sum
    else:
        agreement = 0.0

    data_confidence = data_weighted_confidence / weight_budget if weight_budget > 1e-9 else 0.0

    confidence = max(0.0, min(1.0, 0.5 * abs(final_score) + 0.3 * agreement + 0.2 * data_confidence))

    eps = 0.05
    if final_score > eps:
        direction = "long"
    elif final_score < -eps:
        direction = "short"
    else:
        direction = "neutral"

    return {
        "score": final_score,
        "confidence": confidence,
        "direction": direction,
        "atr": technical.get("atr", 0.0),
        "close": technical.get("close", 0.0),
        "range_high": technical.get("range_high", 0.0),
        "range_low": technical.get("range_low", 0.0),
        "components": components,
        "news_sample": news.get("sample", []),
        "polymarket_sample": polymarket.get("sample", []),
    }
