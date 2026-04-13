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
- Dashboard/API endpoints:
  - `GET /` - dashboard UI
  - `GET /api/state` - deterministic payload with orderbook, pricing, microstructure, market metadata, and diagnostics
  - `GET /api/market-selection` - active/requested asset state
  - `POST /api/market-selection` - queue BTC/ETH switch (applies on market rotation)
  - `GET /api/ws-log`, `GET /api/top10-impact`, `GET /api/reconciliation-log`, `GET /api/brti-ticks`, `GET /api/brti-ws-log`
- Current dashboard modules (`src/ui/static/dashboard/`):
  - `app.js` adaptive poll scheduling + page orchestration
  - `asset_selection.js` BTC/ETH selection UX
  - `charts.js` index + moving-average charts
  - `renderers.js` orderbook and pricing/microstructure rendering
  - `logs.js` verification stream fetching/rendering
  - `format.js` shared formatting helpers
- Polling behavior is visibility-aware and details-panel-aware to reduce unnecessary load.

---

## Project layout (what lives where)

```
src/
├── main.py                     # entrypoint (runs Flask app)
├── core/
│   ├── asset_context.py        # active profile context helpers
│   ├── auth.py                 # Kalshi REST/WS auth + key loading
│   ├── config.py               # env + runtime defaults
│   ├── market_profiles.py      # BTC/ETH profile registry
│   ├── market_selection.py     # persisted active/requested switch state
│   └── settlement.py           # settlement metadata helpers
├── data/
│   ├── kalshi_rest.py          # markets/orderbook REST calls
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
    │   ├── log_routes.py
    │   ├── selection_routes.py
    │   └── state_routes.py
    ├── services/
    │   ├── dashboard_state_service.py
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

- No live order placement module is currently present (intentional during refactor/cleanup).
- No dedicated signal combiner module yet (`engine/signal.py` equivalent still pending).
- No local paper-trading simulator module yet.
- No automated test suite in-repo yet for streamer/pricing/dashboard payload contracts.
- No production deployment package (process supervision, metrics export, alerting, runbook).
