# Kalshi 15-minute crypto trader

Fee-aware systematic trading for Kalshi's single-asset 15-minute crypto
markets, with optional discretionary overrides.

## Quick start

```bash
uv sync
cp .env.example .env
# Generate a token, then set KALSHI_DASHBOARD_TOKEN in .env:
python -c 'import secrets; print(secrets.token_urlsafe(32))'
uv run --env-file .env src/main.py bitcoin
```

Open [http://127.0.0.1:3000](http://127.0.0.1:3000), enter the configured
dashboard token, choose **Sim**, then click **Start**
or **Start Live**. The process starts stopped; live start and live manual orders
require confirmation.

Supported assets: `BTC`, `ETH`, `SOL`, `XRP`, `DOGE`, `BNB`, `ADA`, `NEAR`,
`BCH`, `HYPE`, `TON`, and `ZEC`. Names such as `bitcoin` and `solana` also work.

```bash
uv run --env-file .env src/main.py ETH
uv run --env-file .env src/main.py SOL
```

If the asset is omitted, `KALSHI_MARKET_ASSET` is used, then BTC.

## Credentials and modes

Both modes need a Kalshi API key because the live orderbook WebSocket is
authenticated. Configure the demo or production key selected by `KALSHI_ENV` in
`.env`.

The orderbook uses Kalshi's sequenced WebSocket snapshot and deltas. A sequence
gap pauses book consumers while the same subscription requests a fresh snapshot;
only a failed recovery reconnects the socket.

### Sim and live

**Sim** never calls Kalshi's order-entry API. It:

- starts with an ephemeral $1,000 balance by default;
- simulates IOC fills against displayed live top-of-book price and quantity;
- applies the active Kalshi fee multiplier;
- marks open positions to the live bid;
- supports automated exits, discretionary orders, pause, and flatten; and
- waits for Kalshi's finalized outcome before settling expired positions.

Set `KALSHI_PAPER_STARTING_CASH_CENTS` to change the starting balance. The paper
account resets when the process restarts.

**Start Live** sends IOC orders to the Kalshi environment selected by `KALSHI_ENV`:
`demo` uses Kalshi demo and `prod` uses production. **Stop** disables order
entry. Switching from live to paper also cancels any of this bot's live orders.

## Operating workflow

The engine always submits model entries and exits while it is running. The
dashboard's open-by-default **High-Touch Trading** panel contains model/risk
settings and an IOC ticket for discretionary overrides.

Book, benchmark, and account changes wake a serialized strategy evaluation;
bursts coalesce for 50ms. A one-second timer advances expiry and risk when feeds
are quiet. New buys retain the configured five-second cooldown. Unresolved
live orders block further submissions until reconciliation completes.

Choose Buy or Sell/Reduce, YES or NO, and a contract count in **Click Order**.
These discretionary orders use the active market, top of book, configured
slippage, order and position limits, cash buffer, reduce-only checks, and
daily-loss guard. They do not disable the systematic engine.

### Stop and flatten

- **Stop** stops the engine. In live mode it also cancels this bot's resting
  orders.
- **Flatten Active Market** disarms, cancels, and submits a reduce-only IOC for
  the active position.
- A daily-loss breach disarms and attempts to flatten the active market. The
  lock persists until the next New York trading day.

## How the strategy decides

The model estimates the probability that Kalshi's final-minute settlement
average finishes at or above the rounded contract strike. Prices and accumulated
final-minute fixes come directly from Kalshi's CF Benchmarks feed; there is no
synthetic index or basis adjustment. Before taking liquidity, model value must clear:

- the current marketable IOC price;
- the current Kalshi taker fee;
- configured slippage; and
- the configured minimum net edge.

The desired outcome-side position is the smallest of several ceilings:

- configurable fractional Kelly on current account equity;
- a percentage-of-equity cap for the active market;
- available cash after the cash buffer;
- max order contracts;
- displayed top-of-book quantity; and
- an absolute position-dollar circuit breaker.

The systematic strategy is deliberately taker-only and never posts resting
quotes.

Entries require an initialized, sequence-aligned book on a live stream, fresh
official benchmark data, non-fallback volatility, a known fee policy, synchronized
account state, and at least 20 seconds to expiry. A mathematically locked outcome may
trade inside the cutoff through its lower-edge path. Rolling index and
volatility history are available immediately after rollover; contract-specific
book state resets.

Existing positions exit when the executable bid, after slippage and fees,
exceeds model fair value by the configured edge. Entry gates never block this
edge-reversal exit.

## Dashboard map

- **Autotrader strip:** sim/live mode, session change in NAV, start, stop, and flatten.
- **Positions:** cash, average cost, bid mark, gross mark-to-market P&L, and daily loss capacity.
- **Market & Model:** index, model/market probability, edge, pricing, and
  realized-volatility fit.
- **Order book:** live YES/NO bids, asks, and depth.
- **High-Touch Trading:** model/risk parameters and manual orders.

Contract-specific model state resets at each 15-minute boundary. The socket,
official price history, rolling averages, and realized volatility carry across
markets; account state, daily P&L, risk locks, and settlement tracking continue.
The chart displays Kalshi's trailing 60-second average, while the final-minute
pricer uses Kalshi's separate quarter-hour accumulation.

## Kalshi data ownership

One authenticated socket stays open across market rotation:

- `orderbook_delta`: snapshot and sequenced depth, with in-band gap recovery.
- `cfbenchmarks_value`: 1 Hz reference values, trailing average, and final-minute
  accumulation. Its sample count and window boundaries are used directly.
- `cfbenchmarks_value_5hz`: faster spot updates for BTC, ETH, SOL, XRP, and DOGE.
  These ticks do not enter the 1 Hz history or count as settlement fixes.
- `market_lifecycle_v2`: pauses, terms, close-time changes, settlement, and fee
  override invalidation.
- `fill`, `market_positions`, `user_orders`: live account and execution updates.

REST has distinct jobs: order entry/cancellation, initial market metadata,
initial/reconnect portfolio snapshots, and balance refresh (there is no balance
channel). Balances refresh every five seconds or after account changes.
After an order, streamed fills and positions satisfy position reconciliation;
REST recovers missing notifications or ambiguous HTTP outcomes. Unknown orders
are looked up by the original client ID and never blindly resubmitted. An
unresolved result keeps live entry blocked.

A single `/cfbenchmarks/values?maxResolution=PER_SECOND` read at connection time
seeds recent price history for volatility. If unavailable, the live stream warms
up naturally. It never supplies a fabricated settlement average. Missing or
stale final-minute accumulation pauses model trading.

Series/event fee metadata refreshes at most every five minutes and after relevant
stream events. Paper settlement uses lifecycle results, with a slow REST recovery
check for missed notifications. Full books and ordinary positions are not polled.

Live sizing uses the active market's `exchange_index` balance allocation;
cancellations include the market ticker for automatic shard routing. Missing
shard allocation fails closed. No collateral transfers or automatic rebalancing
are performed by this process.

The exchange adapters, synthetic benchmark calculation, proxy anchoring, and
local settlement forward-fill machinery have been removed. Strategy probability,
volatility estimation, fee-aware sizing, paper simulation, and local risk limits
remain application responsibilities. RFQ, multivariate, Pyth, and order-group
channels are not subscribed because this strategy does not use those products.

### API references checked September 20, 2026

- [Kalshi changelog](https://docs.kalshi.com/changelog)
- [CF 1 Hz and averaging semantics](https://docs.kalshi.com/websockets/cfbenchmarks-value)
- [CF 5 Hz](https://docs.kalshi.com/websockets/cfbenchmarks-value-5hz)
- [CF REST passthrough](https://docs.kalshi.com/cfbenchmarks/rest-passthrough)
- [Historical data](https://docs.kalshi.com/getting_started/historical_data)
- [Historical market candlesticks](https://docs.kalshi.com/api-reference/historical/get-historical-market-candlesticks)
- [Exchange sharding](https://docs.kalshi.com/getting_started/exchange_sharding)
- [REST replication watermark](https://docs.kalshi.com/api-reference/exchange/get-user-data-timestamp)
- [SDK guidance](https://docs.kalshi.com/sdks/overview): Kalshi warns SDKs can lag;
  this project uses the direct API with existing dependencies.


## Historical backtesting

Historical research is split into two layers because model calibration and
execution simulation have different data requirements.

### Model calibration and coarse quote screen

```bash
KALSHI_ENV=prod uv run --env-file .env src/research/backtest.py BTC \
  --max-markets 100 \
  --min-edge-cents 2 \
  --vol-window-seconds 300
```

This backtester merges recent settled markets from the live tier with older
markets from Kalshi's historical tier. CF's `/history/values` endpoint returns
the published historical tick stream and does not expose the `maxResolution`
selector used by the recent-value endpoints. For high-frequency RTIs, replay
therefore uses every historical tick as the production fast spot while filtering
the exact second-boundary publications for realized volatility and settlement
fixes. That reproduces the live separation between
`cfbenchmarks_value_5hz` and `cfbenchmarks_value` without fabricating 1 Hz
fixes from subsecond observations.

Probability calibration is evaluated at fixed horizons including 60, 45, 30,
20, 10, 5, and 1 seconds before close. That is independent of Kalshi quote
candles, so the collapsed final-minute Asian model is actually tested. For
subsecond-capable assets, the report also compares the production fast-spot
probability with the probability obtained from the latest one-second spot.

Outputs are written to `output/backtests/`:

- `calibration.csv`: fixed-horizon model probabilities, outcomes, Brier/log
  loss, known-fix count, and fast-vs-1Hz spot comparison.
- `quote_observations.csv`: model value versus one-minute Kalshi quote closes.
- `market_relative.csv`: at each fixed horizon, compares the model with the
  latest actual non-block Kalshi trade no more than five seconds old. Each market
  contributes at most once per horizon, avoiding activity-weighted scoring.
- `tape_observations.csv`: optional full trade-tape scoring when `--full-tape`
  is requested. It is intentionally off by default because highly active markets
  otherwise dominate the sample.
- `trades.csv`: a deliberately simple first-signal-per-market alpha screen.
- `summary.json`: aggregate/horizon calibration, equal-weight model-versus-market
  proper scores, and the coarse quote screen.

Use `--vol-window-seconds` and `--volatility-scale` to research alternatives
without maintaining a second pricing implementation. The dedicated
`src/research/model_sweep.py` evaluates a small window/scale grid with a
chronological development/holdout split so a parameter choice is selected on
older markets and judged on newer markets.

The live/replay `TradingSettings` also support opt-in research policies without
changing default production behavior:

- `volatility_window_seconds`: realized-volatility lookback (default 300 s).
- `pre_settlement_volatility_window_seconds` and
  `pre_settlement_volatility_scale`: optional longer/more-conservative variance
  policy while time to expiry is above `pre_settlement_until_seconds`.
- `entry_start_seconds_to_expiry`: optional earliest systematic-entry horizon.
- `entry_cutoff_seconds_to_expiry`: ordinary-entry cutoff before close (default
  20 s). Inventory exits and mathematically locked outcomes keep their existing
  special handling.

Together these can isolate a forward-research window without changing defaults.
For example, start=30 and cutoff=1 tests ordinary entries from 30→1 seconds;
start=30 with the default cutoff tests only 30→20 seconds.

These controls are exposed in the dashboard and replayed by
`strategy_backtest.py`. They exist to test the authenticated findings rather
than hard-code a parameter choice from one historical sample.

### Production taker-strategy replay

For the closest replay possible from Kalshi's public historical quote data:

```bash
KALSHI_ENV=prod uv run --env-file .env src/research/strategy_backtest.py BTC \
  --max-markets 100 \
  --starting-cash-cents 100000 \
  --fee-multiplier 1.0 \
  --assumed-top-size 10
```

The replay calls the production `apply_pricing_overrides()` and
`build_trade_signal()` functions directly and executes their IOC signals
through the same `PaperAccount` path used by Sim. It preserves the minimum-edge
and deterministic-edge gates, 20-second entry cutoff, taker-fee logic,
price-range-aware slippage limits, Kelly sizing, bankroll/position/cash caps,
max-order clips, buy cooldown, edge-reversal exits, daily-loss lock/flatten, and
settlement accounting. Market rotation resets the cooldown exactly as the live
runtime does.

Every `TradingSettings` field can be replayed from a JSON file:

```bash
KALSHI_ENV=prod uv run --env-file .env src/research/strategy_backtest.py BTC \
  --settings-json research-settings.json
```

`--min-edge-cents` can still be supplied separately and overrides the JSON
value.

Outputs are written to `output/strategy_backtests/`:

- `decisions.csv`: replayed production-strategy decisions and account state.
- `fills.csv`: simulated IOC fills, including edge-reversal sells.
- `markets.csv`: per-market realized P&L and turnover.
- `summary.json`: portfolio results and an explicit list of exact versus
  approximated replay behavior.

Kalshi only archives quote candles at one-minute minimum resolution, not the
sequenced historical L2 book. The execution replay therefore constructs the
correct YES/NO top of book from each candle close and uses
`--assumed-top-size` only for otherwise unobservable displayed quantity. A
hypothetical fill consumes that assumed liquidity, and the replay permits at
most one new buy per archived minute rather than assuming the quote persisted
through repeated five-second cooldowns. This makes the P&L replay deliberately
conservative, but it still cannot recover latency, queue position, sub-minute
quote changes, historical pause events, exact shard cash, or historical event
fee overrides. Those require recording the live sequenced book.

Both research tools require production Kalshi credentials for CF Benchmarks
history. The passthrough is entitlement-controlled; the backtest fails rather
than substituting another crypto price source when official CF history is
unavailable.

### Forward sub-minute execution research

Public historical candles cannot establish whether the apparent final-seconds
model advantage was actually IOC-fillable. The live streamer can therefore
optionally record raw sequenced market data near expiry:

```bash
KALSHI_RESEARCH_CAPTURE_PATH=.runtime/research_market_data.jsonl
KALSHI_RESEARCH_CAPTURE_HORIZON_SEC=45
```

When enabled, the process appends orderbook snapshots/deltas, CF 1 Hz and 5 Hz
updates, lifecycle/account execution messages, and the production strategy's
decision snapshots during the configured late-market horizon. Records include
receipt time, subscription/sequence identifiers when present, active market,
close time, seconds to expiry, and the original payload. Capture is disabled by
default and does not alter trading decisions.

Analyze a capture with:

```bash
uv run src/research/capture_analysis.py .runtime/research_market_data.jsonl
```

This writes `output/live_capture/decisions.csv` and `summary.json`, including
horizon coverage, signal/reason counts, and detected sequence gaps. It deliberately
does not equate a model signal with an executable fill.

For the dated empirical conclusions from the authenticated BTC research suite,
see [Historical research findings — 2026-09-20](docs/historical_research_2026-09-20.md).

## Default controls

- Minimum taker edge: 2¢ per contract
- Optional earliest-entry horizon: disabled
- Ordinary-entry cutoff: 20 seconds
- Realized-volatility window: 300 seconds
- Optional early-period volatility policy: disabled
- Locked-outcome edge: 0.5¢ per contract
- Kelly fraction: 0.25
- Active-market bankroll cap: 5%
- Max order: 10 contracts
- Absolute position circuit breaker: $50 per outcome side
- Max daily equity loss: $10
- Cash buffer: $25
- Entry cooldown: 5 seconds
- Decision wakeup: market/account events, with a 1-second clock fallback
- IOC tolerance: 1 exchange tick

Settings are process-local. Trading events are appended to
`.runtime/execution_events.jsonl`; the paper and live daily-risk states are kept
separate under `.runtime/`.

## Tests

```bash
uv run --with pytest pytest -q
```
