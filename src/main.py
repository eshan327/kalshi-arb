from data.kalshi_rest import get_open_markets, get_market_orderbook
from core.display import print_orderbook_table, print_market_timing

def main():
    series_tickers = ["KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M"]

    for series in series_tickers:
        print(f"\n{'='*55}")
        print(f"Active markets for Series {series}")
        print(f"{'='*55}")

        try:
            markets = get_open_markets(series)
        except Exception as e:
            print(f"Error fetching markets for {series}: {e}")
            continue

        for market in markets:
            ticker = market['ticker']
            print(f"\nMarket Ticker: {ticker}")
            print(f"Title: {market['title']}")
            
            print_market_timing(market.get('open_time'), market.get('close_time'))
            
            try:
                ob = get_market_orderbook(ticker)
                
                raw_yes = ob.get('yes') or []
                raw_no = ob.get('no') or []
                
                raw_yes_bids = sorted(raw_yes, key=lambda x: x[0], reverse=True)
                raw_no_bids = sorted(raw_no, key=lambda x: x[0], reverse=True)
                
                yes_asks = [[100 - p, q] for p, q in raw_no_bids]
                no_asks = [[100 - p, q] for p, q in raw_yes_bids]

                print_orderbook_table("YES CONTRACT", raw_yes_bids, yes_asks, depth=5)
                print_orderbook_table("NO CONTRACT", raw_no_bids, no_asks, depth=5)

            except Exception as e:
                print(f"  Error parsing orderbook for {ticker}: {e}")

if __name__ == "__main__":
    main()