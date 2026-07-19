from unittest.mock import MagicMock

from bot.exchange.bybit_client import BybitClient


def _make_client():
    client = BybitClient.__new__(BybitClient)  # skip __init__ (would build a real pybit session)
    client.category = "linear"
    client.session = MagicMock()
    client._instrument_cache = {}
    return client


def test_get_closed_pnl_since_single_page():
    client = _make_client()
    client.session.get_closed_pnl.return_value = {
        "retCode": 0, "retMsg": "OK",
        "result": {"list": [{"symbol": "AUSDT", "closedPnl": "1.0"}], "nextPageCursor": ""},
    }
    records = client.get_closed_pnl_since(1_700_000_000_000)
    assert records == [{"symbol": "AUSDT", "closedPnl": "1.0"}]
    client.session.get_closed_pnl.assert_called_once_with(
        category="linear", startTime=1_700_000_000_000, limit=100
    )


def test_get_closed_pnl_since_paginates_until_cursor_empty():
    client = _make_client()
    pages = [
        {"retCode": 0, "result": {"list": [{"symbol": "AUSDT", "closedPnl": "1.0"}], "nextPageCursor": "abc"}},
        {"retCode": 0, "result": {"list": [{"symbol": "BUSDT", "closedPnl": "2.0"}], "nextPageCursor": ""}},
    ]
    client.session.get_closed_pnl.side_effect = pages

    records = client.get_closed_pnl_since(1_700_000_000_000)
    assert records == [
        {"symbol": "AUSDT", "closedPnl": "1.0"},
        {"symbol": "BUSDT", "closedPnl": "2.0"},
    ]
    assert client.session.get_closed_pnl.call_count == 2
    second_call_kwargs = client.session.get_closed_pnl.call_args_list[1].kwargs
    assert second_call_kwargs["cursor"] == "abc"


def test_get_closed_pnl_since_returns_partial_results_on_error():
    client = _make_client()
    from pybit.exceptions import InvalidRequestError

    client.session.get_closed_pnl.side_effect = [
        {"retCode": 0, "result": {"list": [{"symbol": "AUSDT", "closedPnl": "1.0"}], "nextPageCursor": "abc"}},
        InvalidRequestError(request="req", message="boom", status_code=400, time=0, resp_headers=None),
    ]
    records = client.get_closed_pnl_since(1_700_000_000_000)
    assert records == [{"symbol": "AUSDT", "closedPnl": "1.0"}]
