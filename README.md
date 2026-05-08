# kalshi-arb

## Setup

### 1) Install dependencies

- Install `uv` if needed.
- Run `uv sync` from repo root.
- Add packages with `uv add <package_name>` when needed.

### 2) Create credentials

- Create demo keys on `https://demo.kalshi.co` and production keys on `https://kalshi.com`.
- Store private keys in a gitignored folder, for example: `.secrets/demo.txt` and `.secrets/prod.txt`.

### 3) Create `.env`

Create a `.env` in repo root. Runtime settings are loaded from `src/core/config.py`, and auth values are loaded from `src/core/auth.py`.

Bootstrap from the committed template:

```bash
cp .env.example .env
```

Template source: `.env.example`

Recommended `.env` values:

```env
# -----------------------------
# Required auth/env
# -----------------------------
KALSHI_ENV=demo

# used when KALSHI_ENV=demo
KALSHI_DEMO_KEY_ID=demo_key_id_here
KALSHI_DEMO_KEY_PATH=.secrets/demo.txt

# used when KALSHI_ENV=prod
KALSHI_PROD_KEY_ID=prod_key_id_here
KALSHI_PROD_KEY_PATH=.secrets/prod.txt

# optional API/WS overrides
# KALSHI_API_BASE_URL=https://demo-api.kalshi.co/trade-api/v2
# KALSHI_WS_BASE_URL=wss://demo-api.kalshi.co/trade-api/ws/v2

# -----------------------------
# Market selection defaults
# -----------------------------
KALSHI_MARKET_ASSET=BTC
KALSHI_MARKET_SELECTION_STATE_PATH=.runtime/market_selection.json

# -----------------------------
# Execution gate defaults
# -----------------------------
# observe | paper | live
KALSHI_EXECUTION_MODE=paper
KALSHI_EXECUTION_ENABLED=true
KALSHI_EXECUTION_ALLOW_LIVE_IN_DEMO_ENV=false
KALSHI_EXECUTION_LOOP_INTERVAL_SEC=0.25

# -----------------------------
# EV + paper defaults
# -----------------------------
KALSHI_EXECUTION_MIN_EDGE_CENTS=0.5
KALSHI_PAPER_SIM_STARTING_CASH_CENTS=100000

# -----------------------------
# Simulation defaults
# -----------------------------
KALSHI_SIMULATION_OUTPUT_DIR=output
KALSHI_SIMULATION_DEFAULT_N_PATHS=5000
KALSHI_SIMULATION_HORIZON_SECONDS=900
KALSHI_SIMULATION_DEFAULT_STEPS=900
```

Notes:

- Relative key paths are resolved from repo root.
- Only the credential pair for the active `KALSHI_ENV` is used.
- `KALSHI_EXECUTION_MODE` is the environment gate. UI/API can toggle `observe/paper` dynamically, but `live` is only effective when env mode is also `live`.

### 4) Run the app

- Start with `uv run src/main.py`.
- Open `http://127.0.0.1:5000`.
- Startup validates auth and launches background services:
  - market streamer
  - synthetic index aggregator
  - shadow execution loop

### 5) Operator checklist (what to do on your end)

1. Start app and confirm no auth failure on boot.
2. Open dashboard and verify state is updating.
3. Go to Settings tab and click Save Settings once.
4. Confirm mode behavior in runtime status:
   - requested mode is what you selected
   - effective mode respects env gate for live
5. In paper mode, wait for a signal/fill cycle and verify:
   - runtime transitions to `paper_filled` or a clear rejection reason
   - paper ledger equity/unrealized updates
6. Optionally reset ledger with Reset Paper Ledger button.
7. Switch to Simulation tab and click Generate Monte Carlo.
8. Verify interactive charts render and PNG links open.
9. Confirm generated artifacts are written under `output/`.
10. If preparing live execution, set `KALSHI_EXECUTION_MODE=live`, restart app, then set mode to live in Settings.

### API endpoints

Core:

- `GET /` dashboard UI
- `GET /api/state` deterministic aggregate state
- `GET /api/market-selection`
- `POST /api/market-selection`

Logs:

- `GET /api/ws-log`
- `GET /api/top10-impact`
- `GET /api/reconciliation-log`
- `GET /api/brti-ticks`
- `GET /api/brti-ws-log`

Shadow execution + settings:

- `GET /api/settings`
- `POST /api/settings`
- `GET /api/shadow/runtime`
- `GET /api/shadow/events`
- `POST /api/shadow/ledger/reset`

Simulation:

- `POST /api/simulation/generate`
- `GET /api/simulation/latest`
- `GET /output/<path:artifact_path>`

---

## Project layout (what lives where)

```
src/
├── main.py                     # entrypoint (runs Flask app)
├── core/
│   ├── asset_context.py        # active profile context helpers
│   ├── auth.py                 # Kalshi REST/WS auth + key loading
│   ├── config.py               # env + runtime defaults
│   ├── market_metadata.py      # strike extraction helpers
│   ├── market_profiles.py      # BTC/ETH profile registry
│   ├── market_selection.py     # persisted active/requested switch state
│   └── settlement.py           # settlement metadata helpers
├── data/
│   ├── kalshi_rest.py          # markets/orderbook REST calls
│   ├── kalshi_trading.py       # order placement adapter
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
│   ├── shadow/
│   │   ├── runtime.py          # strict mode execution loop (observe/paper/live)
│   │   ├── signal_engine.py    # EV-primary, fee-aware trade signals
│   │   ├── fee_model.py        # taker-fee and EV computations
│   │   ├── fill_model.py       # paper fills crossing live spread + slippage
│   │   ├── paper_ledger.py     # ephemeral paper PnL/accounting
│   │   ├── settings_state.py   # dynamic mutable settings state
│   │   ├── events.py           # event payload builder
│   │   └── models.py
│   ├── simulation/
│   │   ├── gbm_engine.py       # GBM path generation
│   │   ├── replay.py           # Monte Carlo replay through pricing assumptions
│   │   ├── visuals.py          # Plotly HTML + PNG exports
│   │   └── service.py          # simulation orchestrator + payload cache
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
    │   ├── settings_routes.py
    │   ├── simulation_routes.py
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
    │   ├── settings.js
    │   ├── simulation.js
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

## Status (May 2026)

### Implemented

- Strict execution-mode runtime (`observe`, `paper`, `live`) with environment-gated live behavior.
- EV-primary signal engine that emits only when model-vs-market edge remains positive after taker-fee adjustment.
- No default hard liquidity gate; optional `P(book)` hard gate can be enabled dynamically.
- Realistic paper fill model that crosses live spread and applies slippage ticks.
- Ephemeral paper ledger for open positions, average entry, realized/unrealized PnL, equity curve.
- Dynamic settings API (`POST /api/settings`) and Settings panel in dashboard.
- Monte Carlo simulation engine (GBM + replay), Plotly interactive charts, and PNG artifact export under `output/`.
- Simulation tab in dashboard with manual generation and latest-run loading.
- Initial tests for settings-mode resolution, EV signal thresholding, and replay metrics contract.

### Open work

- Expand unit/integration coverage around runtime event sequencing and ledger settlement edge-cases.
- Add richer production hardening: process supervision, alerting, and persistence policy for long unattended runs.
