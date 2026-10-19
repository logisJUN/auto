import time
from unittest.mock import MagicMock

from bot.exchange.bybit_client import BybitClient


def _make_client():
    client = BybitClient.__new__(BybitClient)  # skip __init__ (would build a real pybit session)
    client.category = "linear"
    client.session = MagicMock()
    client._instrument_cache = {}
    return client


def test_get_closed_pnl_since_single_page():
    # Well within one 7-day window -- exercises the single-window path.
    client = _make_client()
    start_ms = int(time.time() * 1000) - 3 * 24 * 3600 * 1000  # 3 days ago
    client.session.get_closed_pnl.return_value = {
        "retCode": 0, "retMsg": "OK",
        "result": {"list": [{"symbol": "AUSDT", "closedPnl": "1.0"}], "nextPageCursor": ""},
    }
    records = client.get_closed_pnl_since(start_ms)
    assert records == [{"symbol": "AUSDT", "closedPnl": "1.0"}]
    client.session.get_closed_pnl.assert_called_once()
    call_kwargs = client.session.get_closed_pnl.call_args.kwargs
    assert call_kwargs["category"] == "linear"
    assert call_kwargs["startTime"] == start_ms
    assert call_kwargs["limit"] == 100
    assert "endTime" in call_kwargs  # now always bounded, not open-ended


def test_get_closed_pnl_since_paginates_until_cursor_empty():
    client = _make_client()
    start_ms = int(time.time() * 1000) - 3 * 24 * 3600 * 1000
    pages = [
        {"retCode": 0, "result": {"list": [{"symbol": "AUSDT", "closedPnl": "1.0"}], "nextPageCursor": "abc"}},
        {"retCode": 0, "result": {"list": [{"symbol": "BUSDT", "closedPnl": "2.0"}], "nextPageCursor": ""}},
    ]
    client.session.get_closed_pnl.side_effect = pages

    records = client.get_closed_pnl_since(start_ms)
    assert records == [
        {"symbol": "AUSDT", "closedPnl": "1.0"},
        {"symbol": "BUSDT", "closedPnl": "2.0"},
    ]
    assert client.session.get_closed_pnl.call_count == 2
    second_call_kwargs = client.session.get_closed_pnl.call_args_list[1].kwargs
    assert second_call_kwargs["cursor"] == "abc"


def test_get_closed_pnl_since_returns_partial_results_on_error():
    client = _make_client()
    start_ms = int(time.time() * 1000) - 3 * 24 * 3600 * 1000
    from pybit.exceptions import InvalidRequestError

    client.session.get_closed_pnl.side_effect = [
        {"retCode": 0, "result": {"list": [{"symbol": "AUSDT", "closedPnl": "1.0"}], "nextPageCursor": "abc"}},
        InvalidRequestError(request="req", message="boom", status_code=400, time=0, resp_headers=None),
    ]
    records = client.get_closed_pnl_since(start_ms)
    assert records == [{"symbol": "AUSDT", "closedPnl": "1.0"}]


def test_get_closed_pnl_since_walks_backward_across_multiple_windows():
    # Bybit enforces startTime/endTime no more than one window apart per
    # call -- a range spanning 3 windows must become 3 separate calls,
    # walking backward from now.
    client = _make_client()
    now_ms = int(time.time() * 1000)
    window_ms = 7 * 24 * 3600 * 1000
    start_ms = now_ms - int(2.5 * window_ms)  # needs 3 windows to fully cover
    client.session.get_closed_pnl.side_effect = [
        {"retCode": 0, "result": {"list": [{"symbol": "AUSDT", "closedPnl": "1.0"}], "nextPageCursor": ""}},
        {"retCode": 0, "result": {"list": [{"symbol": "BUSDT", "closedPnl": "2.0"}], "nextPageCursor": ""}},
        {"retCode": 0, "result": {"list": [{"symbol": "CUSDT", "closedPnl": "3.0"}], "nextPageCursor": ""}},
    ]

    records = client.get_closed_pnl_since(start_ms, window_ms=window_ms)

    assert len(records) == 3
    assert client.session.get_closed_pnl.call_count == 3
    calls = client.session.get_closed_pnl.call_args_list
    # Walks backward: first call's window ends at ~now, each earlier call's
    # endTime matches the previous call's startTime, and the oldest call's
    # startTime is clamped to the originally requested start_ms (not earlier).
    assert calls[0].kwargs["endTime"] == now_ms
    assert calls[1].kwargs["endTime"] == calls[0].kwargs["startTime"]
    assert calls[2].kwargs["endTime"] == calls[1].kwargs["startTime"]
    assert calls[2].kwargs["startTime"] == start_ms


def test_get_available_balance_usdt_parses_field():
    client = _make_client()
    client.session.get_wallet_balance.return_value = {
        "result": {"list": [{"totalAvailableBalance": "123.45"}]},
    }
    assert client.get_available_balance_usdt() == 123.45


def test_get_available_balance_usdt_returns_none_on_missing_field():
    client = _make_client()
    client.session.get_wallet_balance.return_value = {"result": {"list": [{}]}}
    assert client.get_available_balance_usdt() is None


def test_get_available_balance_usdt_returns_none_on_api_error():
    client = _make_client()
    from pybit.exceptions import InvalidRequestError

    client.session.get_wallet_balance.side_effect = InvalidRequestError(
        request="req", message="boom", status_code=400, time=0, resp_headers=None)
    assert client.get_available_balance_usdt() is None
