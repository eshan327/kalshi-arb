# Kalshi 15 minute crypto research

This project collects Kalshi order books, public trades, and CF Benchmarks prices,
then evaluates a settlement-aware probability reference against historical market
outcomes. A separate replay tests candidate taker entries against recorded book
depth. It contains no order entry or account API.

## Setup

Python 3.11+ and `uv` are required.

```bash
uv sync --group dev
cp .env.example .env
```

Set the production API key ID and local RSA private-key path in `.env`.
The historical CF passthrough needs an entitled production key. The recorder
also supports demo credentials when `KALSHI_ENV=demo`; the historical export
requires production. WebSocket connections are authenticated even though the
recorder reads public market data.

## Record a live feed

```bash
uv run --env-file .env src/main.py BTC \
  --capture-path .runtime/market_data.jsonl
```

The command prints a JSON status line every five seconds and appends a JSONL
capture until interrupted. Each line records the local receipt time, session,
and raw Kalshi message. It captures sequenced book snapshots and deltas, public
trades, market lifecycle updates, CF 1 Hz fixes, and CF 5 Hz values when
available. Reconnects open a new session. The process cannot submit orders.
Use `--status-seconds 1` for more frequent console status.

The asset argument accepts the symbols in `src/core/markets.py`.
Only BTC, ETH, SOL, XRP, and DOGE use the 5 Hz CF feed. Every asset uses the
1 Hz CF feed for volatility and the exchange's final minute average.

## Historical study

Use a production key with access to CF Benchmarks history. Export recent settled
markets, non-block public trades, and CF ticks:

```bash
uv run --env-file .env src/backtest.py BTC \
  --max-markets 100 --output-dir output/btc-100
```

For a fixed sample, bound market close times with `--start-close` (inclusive)
and `--end-close` (exclusive), for example
`--start-close 2026-09-01T00:00:00Z --end-close 2026-09-08T00:00:00Z`.
`--horizons 300,120,60,30` changes the evaluation times, and
`--vol-window-seconds 120` changes the reference volatility lookback.
`--max-markets 0` removes the count cap. The selected universe and raw CF
ticks are saved in `source.json` and `cf_ticks.csv` for offline reruns.

The command writes:

| File | Contents |
| --- | --- |
| `source.json` | Market terms, final outcomes, public trades, Kalshi's current historical cutoffs, and any captured quotes |
| `cf_ticks.csv` | Raw published CF timestamps and prices, including subsecond ticks |
| `observations.csv` | A probability and market context at each evaluation horizon, with a train or holdout label |
| `summary.json` | Brier score and log loss for the Asian reference and comparable public trades or captured quote midpoints |

The final 20% of close-time groups are held out. Every horizon for one close
time stays in the same split. Missing CF seconds or final minute fixes reject
the observation; the pipeline does not interpolate them. The exchange's final
result supplies the label. Trade prices are stale-sensitive probability proxies,
not prices you could necessarily trade at. Historical CF timestamps are source
times, not local receipt times, so these rows support forecast research but
cannot establish executable performance.

Replay the saved experiment without an API key or network calls:

```bash
uv run src/backtest.py BTC \
  --input-dir output/btc-100 --output-dir output/btc-100-replay
```

An offline rerun can change horizons, volatility window, or close-time bounds;
it uses only saved data. If the new window reaches before the saved CF ticks,
the missing observations are rejected. Fetch a wider source period for that
study. The summary records the choices used for each run.

If the CF history endpoint returns an authorization error, check that the key
has access to the passthrough. Its history can lag the live feed; the exporter
waits 20 minutes after market close and paces hour reads below the basic read
budget. A large date span can take time and produce a large `cf_ticks.csv`.

## Use captured quotes

Kalshi's historical endpoints provide archived markets, trades, and candles,
but no sequenced order-book archive. Start the recorder before the period you
want to study. It invalidates book state across reconnects and sequence gaps;
replay waits for another snapshot. Keep the recording host's clock synchronized
because quote pairing uses local receipt times. The capture can grow quickly.
To pair its quotes with historical forecasts:

```bash
uv run --env-file .env src/backtest.py BTC \
  --max-markets 100 --capture-path .runtime/market_data.jsonl \
  --output-dir output/btc-captured
```

Only quotes received before each evaluation time and no more than two seconds
old are paired. The exporter saves those quote rows in `source.json`, so an
offline replay does not need the original capture file. The summary compares
forecast accuracy with the captured quote midpoint on exactly the same rows.
This forecast study does not infer fills or profit.

## Test candidate taker entries

Create a CSV with one buy decision per settled market. For example:

```csv
market_ticker,decision_ts,side,contracts,limit_price_cents
KXBTC15M-EXAMPLE,1780000000.250,yes,10,46
```

`decision_ts` is Unix time in seconds on the recorder's clock. Generate it and
the decision from information actually received by that time. The historical
CF export records publication times, not when your program received values;
signals formed from it alone cannot establish executable returns. Fix the
signal and parameters before evaluating a later period.

Run the order book replay with the corresponding historical export and raw
capture:

```bash
uv run src/taker_backtest.py \
  --input-dir output/btc-captured \
  --capture-path .runtime/market_data.jsonl \
  --signals output/signals.csv \
  --output-dir output/taker-study \
  --latency-ms 100 --max-book-age-ms 2000 \
  --depth-fraction 0.5 --fee-multiplier 1 \
  --balance-precision 0.01
```

It writes `fills.csv` with the depth used, partial fills, fees, settlement
payout, and net P&L for each decision; `summary.json` reports train and
holdout totals. Each order acts like a buy Yes or No IOC held to settlement:
the replay walks displayed asks up to the price limit at simulated arrival,
then cancels any unfilled quantity. It rejects stale books, sequence gaps,
and entries after market close. `--depth-fraction` discounts displayed
quantity for a sensitivity run. `--latency-ms` and `--max-book-age-ms` are
research assumptions; measure them before trusting results.

The fee estimate uses Kalshi's current general taker formula,
`0.07 × multiplier × Σ contracts_at_price × price × (1 − price)`, with price
in dollars. It rounds the model fee to a microdollar, then aligns cost plus
fee to the selected balance precision. Use `0.01` for a non-direct member or
`0.0001` for a direct member. Check the [current fee schedule](https://kalshi.com/docs/kalshi-fee-schedule.pdf)
for the series and period studied, and set `--fee-multiplier` accordingly.
Kalshi's [fee rounding rules](https://docs.kalshi.com/getting_started/fee_rounding)
operate per fill with an order accumulator; aggregated public depth cannot
reproduce the individual matches, so this remains an estimate. Quoted depth
is an upper bound on accessible liquidity, not a
guaranteed fill: other orders and network delay can change it before matching.
The replay models one entry per market, settlement exit, and no capital or
portfolio constraints.

## Pricing and implementation

The contract resolves Yes when Kalshi's rounded average of 60 CF Benchmarks
fixes in `(close - 60s, close]` is at least the displayed target. The target
may be the rounded average from the prior quarter-hour boundary. The reference
pricer uses a zero-drift GBM and matches the first two moments of the discrete
arithmetic average to a lognormal law. Inside the final minute it conditions
on the server's known fix count and average. It uses the published rounding
digits to move the effective comparison threshold by half a rounding unit.
The 300 second annualized volatility is a fixed reference convention, not a
fitted forecast. This model is structurally appropriate for the contract's
average payoff, but its diffusion and volatility assumptions have not been
shown to produce an edge after spreads, fees, and latency.

`src/core/` holds contract terms and API signing. `src/data/` holds public REST
reads, authenticated CF reads, WebSocket normalization, the order book, and
capture. `src/pricing/` holds the reference math. `src/backtest.py` builds
forecast datasets; `src/taker_backtest.py` tests supplied entry decisions;
`src/main.py` runs the read-only recorder. There is no selected signal or
fitted trading rule in this repository.

## Checks and local data

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --group dev pytest -q
```

`output/` is ignored and contains disposable research exports. `.runtime/`
holds local feed captures. Older trading-state and execution-log files, if
present from previous versions, are not read by this code.

API references: [Historical data and cutoffs](https://docs.kalshi.com/getting_started/historical_data),
[CF REST history](https://docs.kalshi.com/cfbenchmarks/rest-passthrough),
[CF 1 Hz feed and final minute average](https://docs.kalshi.com/websockets/cfbenchmarks-value),
[CF 5 Hz feed](https://docs.kalshi.com/websockets/cfbenchmarks-value-5hz),
[order book updates](https://docs.kalshi.com/websockets/orderbook-updates),
[public trades](https://docs.kalshi.com/websockets/public-trades), and
[fixed point prices](https://docs.kalshi.com/getting_started/fixed_point_migration).
