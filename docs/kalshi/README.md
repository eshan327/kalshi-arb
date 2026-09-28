# Kalshi API notes

Checked against the [documentation index](https://docs.kalshi.com/llms.txt)
and public production responses on September 28, 2026. Check the current
reference before changing an integration.

## Prediction markets

- [Historical cutoffs](https://docs.kalshi.com/getting_started/historical_data)
  advance separately for markets, trades, orders, and fills. Settled markets
  and trades may need both live and archived endpoints.
- [Live candles](https://docs.kalshi.com/api-reference/market/get-market-candlesticks)
  use `/series/{series_ticker}/markets/{ticker}/candlesticks`.
  [Archived candles](https://docs.kalshi.com/api-reference/historical/get-historical-market-candlesticks)
  use `/historical/markets/{ticker}/candlesticks`.
- The [order book feed](https://docs.kalshi.com/websockets/orderbook-updates)
  starts with a snapshot, then sends sequenced deltas. A gap requires a new
  snapshot. Prediction books publish Yes and No bids. Convert a No bid to a
  Yes ask using `1 - no_bid`.
- Crypto 15-minute contracts in this repo reference CF Benchmarks. Gold,
  silver, copper, platinum, palladium, WTI, and natural gas reference Pyth.
  Read each market's terms and settlement source; the pricer in this repo
  currently covers the CF crypto contracts.

## Perps

- [REST](https://docs.kalshi.com/margin) uses `/trade-api/v2/margin` on the
  standard API host. The Perps WebSocket uses a separate margin host and
  `/trade-api/ws/v2/margin` signing path. WebSocket handshakes require an API
  key, including for public market channels.
- [Market metadata](https://docs.kalshi.com/margin-rest/market/get-market)
  supplies contract size, tick size, trading schedule, and fractional trading
  rules. Perps prices are dollars per contract; the book has bids and asks.
- [Candles](https://docs.kalshi.com/margin-rest/market/get-market-candlesticks),
  [public trades](https://docs.kalshi.com/margin-rest/market/get-trades), and
  [historical funding rates](https://docs.kalshi.com/margin-rest/funding/get-historical-funding-rates)
  are public REST reads. Perps has no historical order-book endpoint; save
  the [WebSocket book](https://docs.kalshi.com/margin-ws/websockets/orderbook-updates)
  before running a depth-based replay.
- [Effective fee rates](https://docs.kalshi.com/margin-rest/fees/get-fee-tiers)
  depend on the account and market. Funding rates and mark prices come from
  the historical funding endpoint. [Risk](https://docs.kalshi.com/margin-rest/risk/get-risk)
  and available-balance reads require eligible account access.
- [Order integration](orders.md) describes the separate explicit order client.

The [Predictions OpenAPI](https://docs.kalshi.com/openapi.yaml),
[Perps OpenAPI](https://docs.kalshi.com/perps_openapi.yaml), and
[changelog](https://docs.kalshi.com/changelog/index) are the source of truth
for endpoint and schema changes.
