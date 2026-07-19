import json

from bot.logger import compute_performance_summary


def _write_decisions(log_dir, events):
    log_dir.mkdir(parents=True, exist_ok=True)
    with open(log_dir / "decisions.jsonl", "w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")


def test_pairs_entry_and_exit_and_computes_win_rate(tmp_path):
    events = [
        {"event": "entry", "symbol": "AAAUSDT", "side": "long",
         "signal": {"confidence": 0.65}, "ts": 1},
        {"event": "exit", "symbol": "AAAUSDT", "reason": "sl_tp_hit", "pnl": 10.0, "ts": 2},
        {"event": "entry", "symbol": "AAAUSDT", "side": "short",
         "signal": {"confidence": 0.9}, "ts": 3},
        {"event": "exit", "symbol": "AAAUSDT", "reason": "signal_reversal", "pnl": -4.0, "ts": 4},
    ]
    _write_decisions(tmp_path, events)

    summary = compute_performance_summary(tmp_path)
    assert summary["overall"]["count"] == 2
    assert summary["overall"]["win_rate"] == 0.5
    assert summary["overall"]["total_pnl"] == 6.0
    assert "0.60-0.70" in summary["by_confidence"]
    assert "0.80-1.00" in summary["by_confidence"]


def test_range_trades_excluded_from_confidence_buckets(tmp_path):
    events = [
        {"event": "entry", "symbol": "BBBUSDT", "side": "long",
         "range_high": 110.0, "range_low": 100.0, "ts": 1},
        {"event": "exit", "symbol": "BBBUSDT", "reason": "sl_tp_hit", "pnl": 2.0, "ts": 2},
    ]
    _write_decisions(tmp_path, events)

    summary = compute_performance_summary(tmp_path)
    assert summary["by_type"]["range"]["count"] == 1
    assert summary["by_type"]["trend"]["count"] == 0
    assert summary["by_confidence"] == {}


def test_unmatched_entry_without_exit_is_ignored(tmp_path):
    events = [
        {"event": "entry", "symbol": "CCCUSDT", "side": "long",
         "signal": {"confidence": 0.7}, "ts": 1},
    ]
    _write_decisions(tmp_path, events)

    summary = compute_performance_summary(tmp_path)
    assert summary["overall"]["count"] == 0


def test_no_log_file_returns_empty_summary(tmp_path):
    summary = compute_performance_summary(tmp_path / "does_not_exist")
    assert summary["overall"]["count"] == 0
    assert summary["trades"] == []
    # by_type must have the same shape as the normal path (trend/range keys
    # with zero-count stats), not an empty dict -- callers index into it
    # unconditionally (e.g. the daily summary email).
    assert summary["by_type"]["trend"]["count"] == 0
    assert summary["by_type"]["range"]["count"] == 0
