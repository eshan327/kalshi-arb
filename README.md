# kalshi-arb

## Setup

### Package Management

- Use `uv` as your Python package manager (download if you don't have it)
- `pyproject.toml` contains all project dependencies
- Running `uv sync` will give you the needed dependencies ez
- Use `uv add [name]` if you need a new package (will update the `.toml`)

### Environment Variables

Create a `.env` file in the repo root. Runtime config is loaded from `src/core/config.py` and credentials from `src/core/auth.py`.

Required variables:

- `KALSHI_ENV` - `demo` or `prod` (default: `demo`)
- `KALSHI_DEMO_KEY_ID` and `KALSHI_DEMO_KEY_PATH` - required when `KALSHI_ENV=demo`
- `KALSHI_PROD_KEY_ID` and `KALSHI_PROD_KEY_PATH` - required when `KALSHI_ENV=prod`

Optional overrides:

- `KALSHI_API_BASE_URL` - overrides REST base URL
- `KALSHI_WS_BASE_URL` - overrides WS base URL
- `KALSHI_MARKET_ASSET` - initial active asset (`BTC` or `ETH`, default `BTC`)
- `KALSHI_MARKET_SELECTION_STATE_PATH` - persisted market-selection state path (default `.runtime/market_selection.json`)

Autonomous execution + analytics overrides:

- `KALSHI_EXECUTION_ENABLED` - enable autonomous order placement loop (`true`/`false`, default `false`)
- `KALSHI_EXECUTION_MODE` - `observe`, `paper`, or `live` (default `paper`)
- `KALSHI_EXECUTION_ALLOW_LIVE_IN_DEMO_ENV` - permit `live` mode when `KALSHI_ENV=demo` (default `false`)
- `KALSHI_EXECUTION_POST_ONLY` - post-only maker behavior (default `true`)
- `KALSHI_EXECUTION_LOOP_INTERVAL_SEC` - loop cadence (default `1.0`)
- `KALSHI_EXECUTION_MIN_CONFIDENCE` - minimum confidence gate where confidence = `|P(model)-0.5|` (default `0.08`)
- `KALSHI_EXECUTION_MIN_EDGE_CENTS` - minimum expected edge to place order (default `1.5`)
- `KALSHI_EXECUTION_MIN_PROBABILITY` / `KALSHI_EXECUTION_MAX_PROBABILITY` - probability exclusion band to avoid marginal bets (defaults `0.08` / `0.92`)
- `KALSHI_EXECUTION_REQUIRE_P_BOOK_CONFIRMATION` - require P(book) confirmation gate (default `true`)
- `KALSHI_EXECUTION_P_BOOK_MIN_QUALITY` - minimum P(book) quality to accept signal (default `0.35`)
- `KALSHI_EXECUTION_P_BOOK_MAX_DIVERGENCE` - max `|P(model)-P(book)|` divergence before reject (default `0.22`)
- `KALSHI_EXECUTION_MAX_ORDER_CONTRACTS` - max contracts per order (default `10`)
- `KALSHI_EXECUTION_MAX_MARKET_CONTRACTS` - max aggregate contracts per market (default `30`)
- `KALSHI_EXECUTION_MAX_DAILY_LOSS_CENTS` - daily loss kill-switch (default `20000`)
- `KALSHI_EXECUTION_EVENTS_PATH` / `KALSHI_EXECUTION_PERFORMANCE_PATH` - JSONL persistence paths (default `.runtime/*`)

Paper activity profile overrides (paper mode only):

- `KALSHI_PAPER_ACTIVITY_PROFILE` - profile name (`balanced` or `high_activity`, default `high_activity`)
- `KALSHI_PAPER_ACTIVITY_PROFILE_ENABLED` - master toggle for high-activity overrides (default `true`)
- `KALSHI_PAPER_ACTIVITY_FORCE_FILL_PER_WINDOW` - force at least one fill attempt late in each market window (default `true`)
- `KALSHI_PAPER_ACTIVITY_FALLBACK_TRIGGER_SEC` - arm fallback when remaining time <= this threshold (default `120`)
- `KALSHI_PAPER_ACTIVITY_FALLBACK_RETRY_SEC` - retry cadence for fallback attempts (default `1.5`)
- `KALSHI_PAPER_ACTIVITY_ORDER_STALE_SEC` - stale timeout before replacing resting paper orders (default `2.5`)
- `KALSHI_PAPER_ACTIVITY_MIN_EDGE_CENTS` - relaxed minimum edge for activity profile (default `0.35`)
- `KALSHI_PAPER_ACTIVITY_MIN_CONFIDENCE` - relaxed confidence gate (default `0.02`)
- `KALSHI_PAPER_ACTIVITY_MIN_TIMING_SCORE` - relaxed timing gate (default `0.0`)
- `KALSHI_PAPER_ACTIVITY_ALLOW_TAKER` - allow ask-taking/bid-taking intent when edge justifies it (default `true`)
- `KALSHI_PAPER_ACTIVITY_TAKER_EDGE_CENTS` - edge threshold for taker intent (default `1.15`)
- `KALSHI_PAPER_ACTIVITY_FALLBACK_MIN_EDGE_CENTS` - minimum edge during fallback attempts (default `0.05`)
- `KALSHI_PAPER_ACTIVITY_FALLBACK_BYPASS_P_BOOK` - bypass P(book) confirmation in fallback mode (default `true`)

P(book) stabilization controls:

- `KALSHI_P_BOOK_OBI_DEPTH`, `KALSHI_P_BOOK_MPP_WINDOW_SEC`, `KALSHI_P_BOOK_TRADE_WINDOW_SEC`
- `KALSHI_P_BOOK_EMIT_INTERVAL_SEC` - cadence gate for public P(book) updates (default `0.5`)
- `KALSHI_P_BOOK_FEATURE_EMA_ALPHA`, `KALSHI_P_BOOK_PROB_EMA_ALPHA`
- `KALSHI_P_BOOK_OBI_CLIP`, `KALSHI_P_BOOK_TFI_CLIP`, `KALSHI_P_BOOK_MPP_CLIP`, `KALSHI_P_BOOK_Z_CLIP`

Paper simulation controls:

- `KALSHI_PAPER_SIM_STARTING_CASH_CENTS` (default `250000`)
- `KALSHI_PAPER_SIM_AGGRESSIVE_FILL_PROB`, `KALSHI_PAPER_SIM_PASSIVE_BASE_FILL_PROB`
- `KALSHI_PAPER_SIM_QUEUE_DECAY`, `KALSHI_PAPER_SIM_QUEUE_AHEAD_FRACTION`
- `KALSHI_PAPER_SIM_MIN_PARTIAL_FILL_FRACTION`, `KALSHI_PAPER_SIM_MAX_PARTIAL_FILL_FRACTION`
- `KALSHI_PAPER_SIM_LATENCY_MS`, `KALSHI_PAPER_SIM_PRICE_DRIFT_CENTS_PER_SEC`, `KALSHI_PAPER_SIM_RANDOM_SEED`

Session export controls:

- `KALSHI_SESSION_EXPORT_ENABLED` (default `true`)
- `KALSHI_SESSION_EXPORT_AUTO_ON_SHUTDOWN` (default `true`)
- `KALSHI_SESSION_EXPORT_DIR` (default `.runtime/sessions`)
- `KALSHI_SESSION_EXPORT_CHART_DPI`, `KALSHI_SESSION_EXPORT_MAX_ROWS`

Notes:

- Relative key paths are resolved from the repo root (not shell cwd).
- Only the credential pair matching `KALSHI_ENV` is used at runtime.

Example `.env`:

```env
KALSHI_ENV=demo

# demo credentials (used when KALSHI_ENV=demo)
KALSHI_DEMO_KEY_ID=demo_key_id_here
KALSHI_DEMO_KEY_PATH=.secrets/demo.txt

# production credentials (used when KALSHI_ENV=prod)
KALSHI_PROD_KEY_ID=prod_key_id_here
KALSHI_PROD_KEY_PATH=.secrets/prod.txt

# optional overrides
# KALSHI_API_BASE_URL=https://demo-api.kalshi.co/trade-api/v2
# KALSHI_WS_BASE_URL=wss://demo-api.kalshi.co/trade-api/ws/v2
# KALSHI_MARKET_ASSET=BTC
# KALSHI_MARKET_SELECTION_STATE_PATH=.runtime/market_selection.json
```

### API Setup

- Create demo keys at `https://demo.kalshi.co` and production keys at `https://kalshi.com`.
- Store private keys in a gitignored folder (for example: `.secrets/demo.txt`, `.secrets/prod.txt`).
- Put the matching key IDs and paths in `.env`.
- Start the app with `uv run src/main.py`; startup validates auth with a balance check.

### Web Dashboard

- Run `uv run src/main.py` from repo root and open `http://127.0.0.1:5000`.
- Startup launches both background services:
  - Kalshi market streamer (`engine/streamer.py`)
  - Synthetic index aggregator (`feeds/brti_aggregator.py`)
  - Autonomous execution loop (`engine/execution/runtime.py`)
- Dashboard/API endpoints:
  - `GET /` - dashboard UI
  - `GET /api/state` - deterministic payload with orderbook, pricing, microstructure, market metadata, and diagnostics
  - `GET /api/market-selection` - active/requested asset state
  - `POST /api/market-selection` - queue BTC/ETH switch (applies on market rotation)
  - `GET /api/ws-log`, `GET /api/top10-impact`, `GET /api/reconciliation-log`, `GET /api/brti-ticks`, `GET /api/brti-ws-log`
  - `GET /api/execution-events`, `GET /api/fill-events`
  - `POST /api/export-session` - manual export of current session artifacts
  - `GET /api/export-sessions` - list previously exported sessions
- Current dashboard modules (`src/ui/static/dashboard/`):
  - `app.js` adaptive poll scheduling + page orchestration
  - `asset_selection.js` BTC/ETH selection UX
  - `charts.js` index, runtime PnL/win-rate/edge, and fill-quality diagnostics charts
  - `renderers.js` orderbook, pricing/microstructure, execution profile, and runtime diagnostics rendering
  - `logs.js` verification stream fetching/rendering
  - `format.js` shared formatting helpers
- Polling behavior is visibility-aware and details-panel-aware to reduce unnecessary load.

### High-Activity Paper Demo

For presentation mode where you want frequent decisions and near-guaranteed per-window participation, use:

```env
KALSHI_EXECUTION_ENABLED=true
KALSHI_EXECUTION_MODE=paper
KALSHI_PAPER_ACTIVITY_PROFILE=high_activity
KALSHI_PAPER_ACTIVITY_FORCE_FILL_PER_WINDOW=true
KALSHI_PAPER_ACTIVITY_FALLBACK_TRIGGER_SEC=120
KALSHI_PAPER_ACTIVITY_ALLOW_TAKER=true
```

Expected runtime behavior in this profile:

- Lower entry gates (edge/confidence/timing) for more order flow
- Intent-aware placement: maker by default, taker when edge is strong enough
- Late-window fallback path that can bypass strict P(book) confirmation to enforce participation
- Dashboard diagnostics for rejection reasons, maker/taker mix, spread/latency bins, and per-window fill rate

---

## Project layout (what lives where)

```
src/
├── main.py                     # entrypoint (runs Flask app)
├── core/
│   ├── asset_context.py        # active profile context helpers
│   ├── auth.py                 # Kalshi REST/WS auth + key loading
│   ├── config.py               # env + runtime defaults
│   ├── market_metadata.py       # strike extraction from market metadata
│   ├── market_profiles.py      # BTC/ETH profile registry
│   ├── market_selection.py     # persisted active/requested switch state
│   └── settlement.py           # settlement metadata helpers
├── data/
│   ├── kalshi_rest.py          # markets/orderbook REST calls
│   ├── kalshi_trading.py       # authenticated order/fill/position/balance wrappers
│   └── kalshi_ws.py            # authenticated WS subscription stream
├── engine/
│   ├── asian_pricer.py         # Asian-style TWAP probability model
│   ├── book_microstructure.py  # OBI/TFI/MPP -> P(book)
│   ├── live_pricing.py         # API-facing pricing snapshot wrapper/cache
│   ├── orderbook.py            # Kalshi YES/NO L2 reconstruction
│   ├── reconciliation.py       # REST vs WS comparison helpers
│   ├── settlement_sampling.py  # deterministic 1Hz sample reconstruction
│   ├── stream_metrics.py       # WS diagnostics logs/counters
│   ├── streamer.py             # market stream runtime + rotation/re-sync
│   ├── twap.py                 # settlement-window tracking + req avg
│   ├── vol_estimator.py        # realized sigma estimation
│   ├── execution/
│   │   ├── models.py           # execution dataclasses
│   │   ├── policy.py           # timing/confidence/edge/sizing policy + P(book) confirmation
│   │   ├── risk.py             # hard risk gates
│   │   ├── metrics.py          # runtime stats and JSONL event recording
│   │   ├── paper_models.py     # paper order/fill/position dataclasses
│   │   ├── paper_simulator.py  # hybrid fill simulator
│   │   ├── paper_account.py    # paper account ledger + mark-to-market
│   │   ├── persistence.py      # JSONL helpers
│   │   └── runtime.py          # observe/paper/live execution orchestrator
│   ├── pricing/
│   │   └── pipeline.py         # pricing pipeline stages
│   ├── market_stream/
│   │   ├── bootstrap.py        # snapshot bootstrap + delta replay
│   │   ├── discovery.py        # market discovery/selection
│   │   ├── display.py          # display-oriented market filters
│   │   └── reconciliation_runner.py
├── feeds/
│   ├── brti_aggregator.py      # feed runtime orchestrator
│   ├── brti_calc.py            # synthetic index math
│   ├── context.py              # feed runtime context wiring
│   ├── calc/
│   │   └── rti_pipeline.py     # profile-aware index calculator wrapper
│   ├── exchanges/
│   │   ├── __init__.py
│   │   ├── base.py             # shared adapter contract/helpers
│   │   ├── bitstamp.py
│   │   ├── coinbase.py
│   │   ├── gemini.py
│   │   ├── kraken.py
│   │   ├── paxos.py
│   │   └── runtime.py          # reconnect/backoff WS runtime
│   ├── state/
│   │   ├── book_store.py       # per-exchange L2 state
│   │   ├── diagnostics_store.py# feed diagnostics logs/counters
│   │   ├── runtime_state.py    # atomic state reset helpers
│   │   └── tick_store.py       # index tick snapshots + settlement proxy
└── ui/
    ├── contracts.py           # API contract keys/shapes
    ├── market_metadata.py     # strike inference helpers
    ├── routes/
  │   ├── export_routes.py
    │   ├── log_routes.py
    │   ├── selection_routes.py
    │   └── state_routes.py
    ├── services/
    │   ├── dashboard_state_service.py
  │   ├── session_export_service.py
    │   └── runtime_services.py
    ├── static/dashboard/
    │   ├── app.js
    │   ├── asset_selection.js
    │   ├── charts.js
    │   ├── format.js
    │   ├── logs.js
    │   └── renderers.js
    ├── templates/dashboard.html
    └── web_app.py
```

---

## Code Guidelines

### Structure

- Everything that matters is under the `src` directory
- We're modularizing everything into subdirectories for a reason, it's more maintainable and organized
- Separate & simplify components as much as you can, market-making project bloated to like a 1500-line `main.py` it was cooked

### Best Practices

- It's best to leave brief comments under both functions & important code blocks so everyone understands your code and knows what does what (important for debugging)
- LLMs are a second resort to reading docs. It's obv useful when on a leash but will bloat the codebase into a mess without clear guidance
- Don't let tech debt accumulate. Read this: https://www.ibm.com/think/topics/technical-debt
- Try to make small, iterative code changes and review/cleanup every change you make before continuing

### Principles

Follow **DRY** (don't repeat yourself), **SOLID** (most important part is Single Responsibility), **KISS** (Keep It Short and Simple) principles, and the **MVC** pattern

---

## Strategy

Kalshi 15m BTC/ETH contracts settle on a final-minute benchmark average (BRTI/ETHUSD_RTI style). This project is building a statistical arbitrage stack around that structure. The core idea:

- **Convergence (Final 60s):** as previous prices get locked into the payout, the outcome becomes more certain
- **Asian Options Pricing (Mins 1-14):** gives us a probabilistic estimate of where the TWAP will land at expiry given the current price and elapsed average.
- **Orderbook Pressure (OBP):** tells us what the market believes and helps us filter/confirm model signals before the final minute.

---

## Roadmap

### Current State (April 2026)

- Runtime architecture is modular and live:
  - Kalshi market stream + reconstructed YES/NO L2 orderbook
  - Multi-exchange synthetic index stream (Coinbase/Kraken/Gemini/Bitstamp/Paxos)
  - Pricing pipeline (`P(model)`) + microstructure signal (`P(book)`) exposed in API/dashboard
- Multi-asset support is active (`BTC`, `ETH`) with queued market switching that applies on market rotation.
- Dashboard/API payloads are deterministic via explicit contracts in `src/ui/contracts.py`.
- Legacy compatibility layers were removed in favor of direct state modules and adapter classes.

### Near-Term Milestones

1. Signal policy layer
  - Define explicit trade policy from `P(model)`, `P(book)`, fees, and risk buffers.
2. Simulation layer
  - Add a local paper simulator/backtester using reconstructed orderbook liquidity and slippage.
3. Risk and execution gate
  - Reintroduce execution only behind hard risk controls and kill-switches.
4. Test coverage and quality gates
  - Add unit/integration tests for stream lifecycle, pricing invariants, and API contracts.
5. Production hardening
  - Service orchestration, persistence strategy, and monitoring/alerting for unattended runtime.

### What's Left

- Signal combiner experimentation remains open (`engine/signal.py` equivalent still pending).
- No automated test suite in-repo yet for streamer/pricing/dashboard payload contracts.
- No production deployment package (process supervision, metrics export, alerting, runbook).
