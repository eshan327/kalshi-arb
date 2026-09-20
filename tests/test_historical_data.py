from __future__ import annotations

from data import kalshi_rest


def test_settled_market_history_merges_live_and_archive_tiers(monkeypatch):
    calls = []

    def pages(path, key, params):
        calls.append((path, key, params))
        if path == "/markets":
            return [
                {"ticker": "RECENT", "result": "yes"},
                {"ticker": "DUP", "result": "no", "source": "live"},
            ]
        if path == "/historical/markets":
            return [
                {"ticker": "OLD", "result": "yes"},
                {"ticker": "DUP", "result": "no", "source": "archive"},
            ]
        raise AssertionError(path)

    monkeypatch.setattr(kalshi_rest, "_public_pages", pages)
    rows = kalshi_rest.get_settled_markets("KXBTC15M")
    by_ticker = {row["ticker"]: row for row in rows}

    assert set(by_ticker) == {"RECENT", "OLD", "DUP"}
    assert by_ticker["RECENT"]["_data_tier"] == "live"
    assert by_ticker["OLD"]["_data_tier"] == "historical"
    # Live copy wins if a moving cutoff briefly exposes the same market in both.
    assert by_ticker["DUP"]["_data_tier"] == "live"
    assert by_ticker["DUP"]["source"] == "live"
    assert calls[0][0] == "/markets"
    assert calls[0][2] == {"series_ticker": "KXBTC15M", "status": "settled"}
    assert calls[1][0] == "/historical/markets"


def test_market_candles_route_to_correct_data_tier(monkeypatch):
    urls = []

    def get_json(url):
        urls.append(url)
        return {"candlesticks": [{"end_period_ts": 1}]}

    monkeypatch.setattr(kalshi_rest, "_get_json", get_json)

    live = kalshi_rest.get_market_candlesticks(
        series_ticker="KXBTC15M",
        ticker="RECENT",
        start_ts=1,
        end_ts=2,
        historical=False,
    )
    archived = kalshi_rest.get_market_candlesticks(
        series_ticker="KXBTC15M",
        ticker="OLD",
        start_ts=1,
        end_ts=2,
        historical=True,
    )

    assert live == archived == [{"end_period_ts": 1}]
    assert "/series/KXBTC15M/markets/RECENT/candlesticks?" in urls[0]
    assert "/historical/markets/OLD/candlesticks?" in urls[1]
