# Kalshi baseline research

A settlement-aware probability baseline for Kalshi 15-minute crypto markets, shared
by historical evaluation and a guarded live/paper trader. This is a research
foundation; the baseline and trading policy are not validated alpha.

## Run

```bash
uv sync --group dev
cp .env.example .env
# Configure your Kalshi key and a random dashboard token of at least 32 characters.
uv run --env-file .env src/main.py BTC
```

Open [localhost:3000](http://127.0.0.1:3000), authenticate, and select **Sim** or
**Live**. The engine starts stopped. Live orders use the environment in `.env`;
Sim uses an ephemeral paper account against the live top of book. Stop disables
entry; Flatten submits a reduce-only IOC. Paper fills omit network latency.

Both modes require authenticated market feeds. Settings cover net edge, bounded
fractional Kelly sizing, order/position/cash limits, daily loss, cooldown, slippage,
and the entry cutoff. The dashboard shows baseline state without model overrides.

## The frozen baseline

`src/pricing/baseline.py:compute_pricing_snapshot` consumes only official CF spot,
one-second benchmark history, market strike/rounding terms, known settlement fixes,
and an explicit evaluation timestamp. Live calls enter through `live_pricing.py`.

- Volatility is the annualized sample standard deviation of one-second log returns
  over the last 300 seconds (`vol_estimator.py`). A complete exact-second grid is
  required. Missing history means no forecast; zero observed variance stays zero.
  The window is a baseline convention, not an optimized parameter.
- `asian_pricer.py` assumes zero-drift GBM and moment-matches the arithmetic average
  to a lognormal distribution. Before settlement it prices all 60 future fixes;
  inside the final minute it conditions on the known sum and actual remaining fix
  times, including fractional seconds.
- The fixing interval is `(close - 60s, close]`. Published rounding digits shift the
  comparison threshold by half a rounding unit. Profile rounding defaults apply
  when the exchange omits that metadata. Kalshi's final result supplies the label.
- The 5 Hz stream updates spot only. The 1 Hz stream owns volatility and settlement
  averages. Replay uses exact-second publications; it never floors 200 ms ticks.

The baseline has no quote inputs, calibration, volatility scaling, momentum,
entry-timing forecast adjustment, or learned features.

## Generate a dataset

```bash
uv run --env-file .env src/backtest.py BTC \
  --max-markets 100 --output-dir output/baseline

# Reproduce the same rows without network access or credentials:
uv run src/backtest.py BTC \
  --input-dir output/baseline --output-dir output/replay
```

Fetching requires production credentials (`KALSHI_ENV=prod`). Recent and archived
markets/trades are merged and paginated; CF history is fetched once per hour.
Source data and outputs are:

| File | Purpose |
| --- | --- |
| `source.json` | Market terms, outcomes, public trades, retrieval time |
| `cf_ticks.csv` | Published CF prices and original timestamps |
| `observations.csv` | One baseline row per market/horizon, with train/holdout labels |
| `summary.json` | Brier/log loss, paired market comparisons, rejections, conventions |

Rows include spot, strike/rounding threshold, volatility, known-fix count, partial
average, required remaining average, and recent market-trade probability with its
age. Missing or stale market comparisons stay null. Metrics compare model and
market on exactly the same paired rows. The last 20% of market close-time groups
form the holdout; a market's horizons never cross the split. At least two valid
close-time groups are required. Missing CF windows fail or appear as rejected
observations; nothing is interpolated or fabricated.

Public trades are probability proxies, not executable quotes. Historical vendor
timestamps cannot reconstruct feed latency, historical revisions, or sequenced L2.
This harness reports forecast accuracy, not simulated fills or profitability.

For future work, reuse `pricing_at`/`evaluate_market` to reconstruct state, freeze
the exported dataset and split, develop candidates on training rows, and use
`score_predictions` for the same held-out scoring. Keep candidates in research;
changing ingestion or execution is unnecessary. Do not tune on the holdout.

## Code map and safety

| Location under `src/` | Responsibility |
| --- | --- |
| `core/` | Configuration, authentication, market profiles and terms |
| `data/` | REST/WebSocket adapters, benchmark history, orderbook, account state |
| `pricing/` | Baseline forecast, Asian math, realized volatility, live snapshot cache |
| `trading/` | Fee-aware decisions, limits, execution and paper accounting |
| `backtest.py` | Historical reconstruction, dataset export and chronological scoring |
| `ui/` | Dashboard and its read-only snapshot |

Sequence gaps invalidate the book until recovery. Stale/disconnected feeds,
unknown fees, unsynchronized accounts, and ambiguous submissions block trading.
IOC sell orders are reduce-only; unresolved live orders reconcile by the existing
client ID instead of being resent. Daily-loss state and execution audit events
remain separate from research. Reserve cash and sizing use the worst permitted
IOC price including fees. Default risk/edge parameters have not been optimized.

```bash
uv run --group dev pytest -q
```

## Local files

- `.venv/` is the Python environment managed by `uv sync`; keep it while working.
- `.runtime/web/` is Reflex's generated frontend and npm dependencies;
  `.runtime/states/` holds dashboard sessions. Reflex recreates both.
- `.runtime/trading_state.json` preserves the live daily-loss guard across restarts;
  its `.paper` sibling is separate. `execution_events.jsonl` is the trading audit.
  Do not clear these as build caches.
- `output/baseline/` holds the saved research dataset. New runs use `--output-dir`.
- `assets/dashboard.css` and `rxconfig.py` are Reflex inputs. `reflex.lock/` pins
  frontend dependencies; `uv.lock` pins Python dependencies. Commit both locks.
- `.github/workflows/` runs offline safety checks and an optional manual dataset export.

Old simulation reports, superseded research notes and abandoned package trees are
removed. Tests focus on pricing/replay correctness, feed recovery, authentication
and trading safety. Test runs disable pytest's disk cache; use
`PYTHONDONTWRITEBYTECODE=1 uv run pytest -q` to avoid Python bytecode too.

API references: [historical data](https://docs.kalshi.com/api-reference/historical/get-historical-cutoff-timestamps),
[CF REST history](https://docs.kalshi.com/cfbenchmarks/rest-passthrough),
[1 Hz and settlement averages](https://docs.kalshi.com/websockets/cfbenchmarks-value),
[5 Hz spot](https://docs.kalshi.com/websockets/cfbenchmarks-value-5hz),
[WebSockets](https://docs.kalshi.com/websockets).
