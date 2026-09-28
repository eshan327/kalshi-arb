# Kalshi market research

Tools for studying Kalshi 15-minute markets and Kalshi Perps. The repo records
live order books, exports public history, evaluates a crypto settlement-price
reference, and replays externally generated taker signals. Supported markets
include crypto, metals, WTI oil, and natural gas.

## Setup

Python 3.11+ and `uv` are required.

```bash
uv sync --group dev
cp .env.example .env
```

Set `KALSHI_ENV=prod`, your API key ID, and the local RSA or Ed25519 private-key
path in `.env`. Kalshi requires authentication for WebSocket connections. The CF
Benchmarks historical feed also requires an entitled key. Public REST history
can be fetched without credentials.

## Record order books

Record one 15-minute series:

```bash
uv run --env-file .env src/main.py GOLD \
  --capture-path .runtime/gold.jsonl
```

The same command accepts the assets in [src/core/markets.py](src/core/markets.py).
Crypto captures include CF Benchmarks prices where available. Metals and energy
captures contain Kalshi books and trades; their settlement source is Pyth.

Discover and record a Perps ticker:

```bash
uv run --env-file .env src/perps.py --list-markets
uv run --env-file .env src/perps.py KXGOLDPERP \
  --capture-path .runtime/gold-perp.jsonl
```

Both recorders append raw WebSocket messages with local receipt timestamps and
session markers. Reconnects start new sessions. Keep the recording clock in sync
and start capture before the study period. Use a separate file for each recorder.

## Export historical data

Export settled 15-minute markets by close time, including terms, outcomes,
public trades, and one-minute candles:

```bash
uv run --env-file .env src/historical_data.py 15m GOLD \
  --start 2026-09-28T04:45:00Z --end 2026-09-28T04:46:00Z \
  --output-dir output/gold-15m
```

`--start` is inclusive and `--end` is exclusive. `source.json` contains the
market universe; `trades.jsonl` and `candles.jsonl` contain the time series.
The exporter selects Kalshi's live or archived endpoint using its current
historical cutoff. `summary.json` counts missing minute candles.

Export Perps candles, public trades, funding rates, and market metadata:

```bash
uv run --env-file .env src/historical_data.py perps KXGOLDPERP \
  --start 2026-09-28T04:30:00Z --end 2026-09-28T04:46:00Z \
  --output-dir output/gold-perp-history
```

The Perps manifest counts missing minute candles. Missing candles are preserved
as gaps. Market objects in both exports are snapshots fetched after the study
period. Use their terms and outcomes as metadata and labels, not their current
prices, volume, or open interest as historical features.

## Replay taker signals

Signals are CSV files produced by your strategy. Use information received by
`decision_ts`; the replay does not generate a strategy.

For a 15-minute market, use one entry per market:

```csv
market_ticker,decision_ts,side,contracts,limit_price_cents
KXGOLD15M-26SEP280045-45,1790570640.25,yes,10,46
```

```bash
uv run src/taker_backtest.py \
  --input-dir output/gold-15m --capture-path .runtime/gold.jsonl \
  --signals output/gold-signals.csv --output-dir output/gold-replay \
  --latency-ms 100 --max-book-age-ms 2000 \
  --depth-fraction 0.5 --fee-multiplier 1 --balance-precision 0.0001
```

This replay walks captured asks at simulated order arrival, allows partial
fills, estimates taker fees, and holds filled contracts to settlement. Check
the [fee schedule](https://kalshi.com/docs/kalshi-fee-schedule.pdf) for the
series and dates being tested. Direct-member balance precision is `$0.0001`;
use `--balance-precision 0.01` for an FCM account.

For Perps, provide entry and exit decisions with limits in dollars per contract:

```csv
ticker,entry_ts,exit_ts,side,contracts,entry_limit_dollars,exit_limit_dollars
KXGOLDPERP,1790570400.25,1790570460.25,long,1,4.50,4.00
```

```bash
uv run src/perps_backtest.py \
  --history-dir output/gold-perp-history \
  --capture-path .runtime/gold-perp.jsonl \
  --signals output/perps-signals.csv --output-dir output/perps-replay \
  --taker-fee-rate 0.001 --latency-ms 100 \
  --max-book-age-ms 2000 --depth-fraction 0.5
```

Perps replay requires full-size entry and exit fills. It reports failed exits
as open positions and applies published funding rates at simulated execution
times. Set `--taker-fee-rate` to the applicable account rate for the period.

Both replays need matching book captures. Kalshi's historical trades and candles
do not reconstruct executable depth. Displayed depth may disappear before an
order reaches the exchange. The Perps replay does not simulate margin or
liquidation. Prediction captures made before the native-bid format marker need
to be recorded again.

## Run a mixed study

Your signal code can use either product's history to produce actions in both
signal files. A study runs any number of 15-minute or Perps legs with one
holdout boundary. Save this as `study.json` beside the paths it references:

```json
{
  "holdout_start": "2026-09-28T04:45:00Z",
  "latency_ms": 100,
  "max_book_age_ms": 2000,
  "depth_fraction": "0.5",
  "legs": [
    {
      "name": "gold15m", "product": "15m",
      "history_dir": "output/gold-15m", "capture_path": ".runtime/gold.jsonl",
      "signals_path": "output/gold-signals.csv", "fee_multiplier": "1"
    },
    {
      "name": "goldperp", "product": "perps",
      "history_dir": "output/gold-perp-history",
      "capture_path": ".runtime/gold-perp.jsonl",
      "signals_path": "output/perps-signals.csv", "taker_fee_rate": "0.001"
    }
  ]
}
```

```bash
uv run src/strategy_backtest.py --config study.json --output-dir output/study
```

Each leg keeps its detailed replay files. `results.csv` and `summary.json`
combine scored PnL and show unscored or open positions. The train split ends
before the boundary; holdout decisions start at or after it. Trades spanning
the boundary appear in `crossing`. Legs replay independently, with no shared
capital or liquidation model.

## Evaluate the crypto price reference

The CF Benchmarks model estimates the probability of a 15-minute crypto market
settling Yes. It requires historical CF access:

```bash
uv run --env-file .env src/backtest.py BTC \
  --max-markets 100 --output-dir output/btc-100
uv run src/backtest.py BTC \
  --input-dir output/btc-100 --output-dir output/btc-100-replay
```

The export saves market terms, public trades, CF ticks, observations, and
train/holdout scores. `--start-close` and `--end-close` bound the sample;
`--horizons` and `--vol-window-seconds` set evaluation times and volatility
lookback. Outcomes unavailable before the first holdout observation go into
`crossing`. Historical CF timestamps are publication times, so use recorded feed
receipt times for execution studies. The current probability model covers CF
crypto markets; Pyth-settled markets need their own model.

## Other commands and references

`src/perps.py --account` reads Perps account eligibility, balances, positions,
and risk. [Order integration](docs/kalshi/orders.md) covers the explicit REST
order client. Research commands only read data.

Run checks with `uv run --group dev pytest -q`. `output/` and `.runtime/` are
ignored local data directories.

Kalshi references: [historical data](https://docs.kalshi.com/getting_started/historical_data),
[prediction order books](https://docs.kalshi.com/websockets/orderbook-updates),
[Perps API](https://docs.kalshi.com/margin), and
[API notes](docs/kalshi/README.md).
