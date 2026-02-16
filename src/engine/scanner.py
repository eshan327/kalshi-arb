from data.kalshi_rest import get_open_markets, get_market_orderbook
from data.orderbook_math import parse_orderbook
from core.display import print_orderbook_table, print_market_timing
from execution.order_manager import execute_trade_decision

# ! File is probably redundant, keep for reference.

def run_market_scanner(client):
    """Displays active crypto markets and checks for execution signals."""

    series_tickers = ["KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M"]
    test_signaled = False

    for series in series_tickers:
        print(f"\n{'='*55}")
        print(f"Active markets for Series: {series}")
        print(f"{'='*55}")

        markets = get_open_markets(series)
        if not markets:
            continue

        for market in markets:
            ticker = market['ticker']
            print(f"\nMarket Ticker: {ticker}")
            print(f"Title: {market['title']}")
            
            print_market_timing(market.get('open_time'), market.get('close_time'))
            
            # Simulated trigger: fire a trade signal exactly once
            if not test_signaled and series == "KXBTC15M":
                execute_trade_decision(client, ticker, "yes", 1)
                test_signaled = True

            # View the orderbook
            try:
                ob = get_market_orderbook(ticker)
                yes_bids, yes_asks, no_bids, no_asks = parse_orderbook(ob)

                print_orderbook_table("YES CONTRACT", yes_bids, yes_asks, depth=5)
                print_orderbook_table("NO CONTRACT", no_bids, no_asks, depth=5)
            except Exception as e:
                print(f"  Error displaying orderbook for {ticker}: {e}")