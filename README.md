# Kalshi market research

Tools for researching Kalshi 15-minute markets and Perps across crypto, metals,
WTI oil, and natural gas. The repo records live order books, exports historical
data, and tests trade signals. It also includes a probability model for crypto
contracts that settle against CF Benchmarks prices.

A typical study starts with recorded books and historical data. You supply trade
signals as CSV files, then replay them to estimate fills and PnL with latency and
fees.

## Setup

Requires Python 3.11+ and `uv`.

```bash
uv sync --group dev
cp .env.example .env
```

For live recording, set `KALSHI_ENV=prod`, `KALSHI_PROD_KEY_ID`, and
`KALSHI_PROD_KEY_PATH` in `.env`. The path should point to your RSA or Ed25519
private key. Public REST history needs no credentials. The crypto model's
historical evaluation requires a key with CF Benchmarks historical-feed access.

Record a 15-minute market series:

```bash
uv run --env-file .env src/main.py GOLD --capture-path .runtime/gold.jsonl
```

Supported assets are listed in [src/core/markets.py](src/core/markets.py).
Recordings go in `.runtime/`; exports and study results usually go in `output/`.
Both directories are ignored by Git. Exports and replays require a new output
directory.

## Main scripts

| Script | Purpose |
| --- | --- |
| `src/main.py` | Record 15-minute market books and print pricing updates. |
| `src/perps.py` | Discover Perps markets, record a ticker, or read account state. |
| `src/historical_data.py` | Export market history, including trades and candles. |
| `src/backtest.py` | Evaluate the crypto probability model against settled outcomes. |
| `src/taker_backtest.py` | Replay 15-minute entries and hold fills to settlement. |
| `src/perps_backtest.py` | Replay Perps entries and exits, including funding. |
| `src/strategy_backtest.py` | Combine replays into a study with a shared holdout date. |

Run a script with `--help` for its arguments. Shared feed and API code lives in
`src/data/`, pricing in `src/pricing/`, and crypto model evaluation in
`src/backtesting/`. Configuration, authentication, and asset definitions are in
`src/core/`.

Fill replays need book captures from the period being tested, with the same
exchange environment and endpoint as the historical export. Trades and candles
alone cannot reconstruct depth. Fills are estimates; Perps replays omit margin
and liquidation, and study legs have no shared capital model.

The research scripts read data. The separate [order client](docs/kalshi/orders.md)
can submit and manage orders when explicitly called. See the
[API notes](docs/kalshi/README.md) for integration details and settlement sources.

Run tests with `uv run --group dev pytest -q`.
