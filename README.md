# kalshi-arb

## Setup

### Package Management

- Use `uv` as your Python package manager (download if you don't have it)
- `pyproject.toml` contains all project dependencies
- Running `uv sync` will give you the needed dependencies ez
- Use `uv add [name]` if you need a new package (will update the `.toml`)

### Environment Variables

Your `.env` file should look like this:

```env
# defaults for our runs (real markets, no active trading yet)
KALSHI_ENV=prod
KALSHI_EXECUTION_MODE=OBSERVE

# demo credentials
KALSHI_DEMO_KEY_ID=[i almost forgot to delete mine from this readme when committing ts]
KALSHI_DEMO_KEY_PATH=.secrets/demo.txt

# production credentials
KALSHI_PROD_KEY_ID=[another string of alphanumeric characters]
KALSHI_PROD_KEY_PATH=.secrets/prod.txt
```

### API Setup

- Get both prod and demo keys in account/security settings (demo url: https://demo.kalshi.co)
- Just nickname them `prod` and `demo` so your files are `prod.txt` and `demo.txt`
- Put the `.txt` files under a gitignored folder called `.secrets`
- The `KEY_ID` is copypasteable from Kalshi settings, put them in the `.env`
- Pls make sure everything is gitignored properly or I lowk steal your bank account

### Web Dashboard

- Run `python src/main.py` from the repo root and open `http://127.0.0.1:5000`.
- **`src/ui/web_app.py`** — Flask app; **`GET /api/state`** returns orderbook, BRTI, **`pricing`** (Asian/collapsed pricer + vol) and **`microstructure`** (OBI / TFI / MPP / `P_book`).
- **`src/ui/templates/dashboard.html`** + **`src/ui/static/dashboard/`** modules — live UI.
- The dashboard shows:
  - Live reconstructed YES/NO top-10 orderbook tables (in-memory state).
  - BRTI hero number, synthetic 60s settlement proxy, BRTI spot + 60s moving-average charts.
  - **Asian pricer & Realized Vol (Live)** — `P(model)` and `P(book)` bar gauges (green if above 50% YES-lean, red if below 50% NO-lean), metric grid (time to expiry, strike, σ, spot, **TWAP / window**, optional **Req. avg (rest)**). **TWAP / window** describes progress inside Kalshi’s final 60s: not in the last minute yet, accumulating samples, or partial TWAP + `k/60` seconds locked in.
  - Collapsible **Read me — what P(model) and P(book) mean** (plain-language readme).
  - Right column: **Asian Pricer and Realized Vol Calculations** — pretty-printed JSON (`pricing`, `microstructure`) each poll.
  - **Explain technical metrics** — websocket counters and notes.
  - Verification streams: reconciliation, BRTI ticks, Kalshi raw logs, etc.

---

## Project layout (what lives where)

```
src/
├── main.py                 # entry: starts Flask dashboard
├── core/
│   ├── config.py           # env, API/WS URLs, dashboard defaults
│   ├── auth.py             # Kalshi REST/WS auth
│   ├── market_profiles.py  # BTC/ETH market profile registry
│   ├── market_selection.py # persisted active/requested asset switch state
│   ├── asset_context.py    # normalized active profile context helpers
│   └── settlement.py       # settlement rule/window metadata helpers
├── data/
│   ├── kalshi_rest.py      # markets, orderbook snapshots
│   └── kalshi_ws.py        # subscribe to orderbook + ticker
├── engine/
│   ├── streamer.py         # stream orchestrator + public state accessors
│   ├── orderbook.py        # Kalshi YES/NO L2 reconstruction
│   ├── twap.py             # 60s settlement window, discrete samples, required avg
│   ├── asian_pricer.py     # Levy branch + collapsed-variance binary TWAP vs strike
│   ├── vol_estimator.py    # realized σ from BRTI ticks
│   ├── live_pricing.py     # compatibility wrapper for pricing pipeline
│   ├── settlement_sampling.py # shared deterministic 1Hz sample reconstruction
│   ├── pricing/
│   │   └── pipeline.py     # pricing pipeline stages and snapshot assembly
│   ├── market_stream/
│   │   ├── discovery.py    # market close parsing/selection
│   │   ├── bootstrap.py    # REST bootstrap + delta replay helpers
│   │   ├── display.py      # actionable display-level filtering
│   │   └── reconciliation_runner.py # reconciliation routine
│   ├── book_microstructure.py  # OBI, TFI, MPP → sigmoid P(book); trade hook for TFI
│   ├── stream_metrics.py   # WS audit logs
│   └── reconciliation.py   # REST vs WS level checks
├── feeds/
│   ├── brti_calc.py        # compatibility BRTI math entrypoint
│   ├── brti_state.py       # compatibility state facade (re-exports modular stores)
│   ├── brti_aggregator.py  # feed runtime orchestrator
│   ├── context.py          # explicit feed runtime context
│   ├── calc/
│   │   └── rti_pipeline.py # profile-aware BRTI calculator wrapper
│   ├── state/
│   │   ├── book_store.py   # per-exchange L2 book state
│   │   ├── tick_store.py   # index tick/state snapshots + settlement proxy
│   │   ├── diagnostics_store.py # feed diagnostics logs/counters
│   │   └── runtime_state.py # atomic feed-state reset helpers
│   └── exchanges/          # adapter-based Coinbase/Kraken/Gemini/Bitstamp/Paxos streams
├── execution/
│   └── order_manager.py    # place orders (paper/live hooks)
└── ui/
  ├── web_app.py          # Flask app bootstrap + route registration
  ├── contracts.py        # API response contract keys/shapes
  ├── routes/
  │   ├── state_routes.py
  │   ├── selection_routes.py
  │   └── log_routes.py
  ├── services/
  │   ├── dashboard_state_service.py
  │   └── runtime_services.py
    ├── market_metadata.py  # infer strike from Kalshi market dict
    ├── templates/dashboard.html
  └── static/dashboard/   # format, selector, charts, renderers, logs, app coordinator
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

Kalshi's 15m BTC contracts settle on a TWAP of the BRTI over the final 60 seconds. We are building a **Statistical Arbitrage bot**. This means:

- **Convergence (Final 60s):** as previous prices get locked into the payout, the outcome becomes more certain
- **Asian Options Pricing (Mins 1-14):** gives us a probabilistic estimate of where the TWAP will land at expiry given the current price and elapsed average.
- **Orderbook Pressure (OBP):** tells us what the market believes and helps us filter/confirm model signals before the final minute.

---

## Roadmap

### What's Built

**Authentication & Config (`src/core/`)**
- `config.py` and `auth.py` — environment variables and Kalshi authentication

**Data Infrastructure (`src/data/`)**
- `kalshi_rest.py` — markets list, per-market orderbook snapshots
- `kalshi_ws.py` — authenticated WebSocket subscribe (orderbook deltas + ticker)

**Engine (`src/engine/`)**
- `streamer.py` — async loop for KXBTC15M: REST snapshot, delta replay, sequence reconciliation, rotate to next market on close. Thread-safe **`_live_market_info`** for Flask. On **new market ticker**: resets **`live_pricing`** TWAP session and **`book_microstructure`** so the next 15m window keeps calculating cleanly.
- `orderbook.py` — Kalshi YES/NO book from snapshot + ordered deltas
- `twap.py` — 60s settlement window alignment with discrete 1 Hz samples, partial average, required average for rest of window
- `asian_pricer.py` — binary P(TWAP above K): Levy-style when more than 60s remain; collapsed-variance inside the last 60s
- `vol_estimator.py` — realized (and optional EWMA) annualized σ from BRTI history
- `live_pricing.py` — `compute_live_pricing_snapshot()` for API; TWAP key includes **`close_time`** so back-to-back contracts do not reuse state
- `book_microstructure.py` — OBI, TFI, MPP, `P_book`; `on_live_orderbook_update` from streamer; `on_public_trade` for future trade feed
- `stream_metrics.py`, `reconciliation.py` — logging and REST vs WS checks

**Feeds (`src/feeds/`)**
- `brti_calc.py` — BRTI pipeline per CME-style methodology
- `brti_state.py` — global exchange books + BRTI tick history
- `brti_aggregator.py` — multi-exchange asyncio tasks + periodic BRTI
- `exchanges/*.py` — per-exchange websocket feeds

**UI (`src/ui/`)**
- `web_app.py` — Flask + background asyncio (streamer + BRTI aggregator)
- `market_metadata.py` — `extract_suggested_strike()` from market payload

**Execution (`src/execution/`)**
- `order_manager.py` — order placement helpers

---

### What's Left

Originally, we were catching data but the bot didn't "see" the market state. Here is the build order. *Tasks 1 and 2 can be done in parallel.* **This plan is speculative; modify as needed.**

#### Task 1: Orderbook Reconstruction (done)

We can't trade on isolated WebSocket messages. We need to build `src/engine/orderbook.py`. It needs to:

1. Fetch a REST snapshot on startup.
2. Buffer incoming WS delta events from `streamer.py`.
3. Filter stale sequence IDs and apply deltas to the snapshot in order.
4. Expose a clean `get_orderbook()` interface so our math engine can read the live state.

*This is tentatively finished*

#### Task 2: BRTI Proxy (done)

Kalshi settles on the CF Benchmarks BRTI, and we need to synthesize the BRTI without direct API access.

1. Create `src/feeds/` directory.
2. Build WS connections to Coinbase, Kraken, Gemini, Bitstamp, Paxos (see `feeds/exchanges/`).
3. Build `brti_aggregator.py` to collect multi-exchange orderbook data and output a synthetic BRTI-style real-time index (`brti_calc.py`).

*This is tentatively finished.*

#### Task 3: Asian Options Pricer & Volatility (done)

We have the TWAP tracker but we need to price the uncertainty of the remaining time.

1. **`asian_pricer.py`** — Levy-style / collapsed-variance probability for the 60s TWAP vs strike (`N(d2)` / collapsed Gaussian branch).
2. **`vol_estimator.py`** — realized annualized σ from BRTI log returns.
3. **`live_pricing.py`** — combines BRTI, TWAP state, vol, and pricer for `GET /api/state`.

**Dashboard:** below the BRTI / MA charts, **Asian pricer & Realized Vol (Live)** shows bar gauges for **P(model)** and **P(book)** (green when probability is above 50% YES, red below), plus a metric grid. In the **right** column, **Asian Pricer and Realized Vol Calculations** exposes `pricer_detail`, σ fields, and microstructure (`obi`, `tfi`, `mpp`, `z`) on every poll.

#### Task 4: OBP Signal

Build `src/engine/obp.py` to quantify directional conviction (bid/ask imbalance at the top levels, size-weighted mid, etc.). **Partial precursor:** `book_microstructure.py` already exposes top-N **OBI**, **TFI**, **MPP**, and **P(book)** on each orderbook update; a dedicated `obp.py` may consolidate or extend this.

#### Task 5: Signal Combiner

Build `src/engine/signal.py`. Merge the fair value from the Asian pricer with the conviction from the OBP. Calculate the edge minus taker fees. If `edge > min_edge` and `obp_aligned == True`, emit a `TradeSignal`.

#### Task 6: Paper Trading Simulator

Kalshi's demo markets are illiquid and useless for high-frequency testing. We need a local sim built into `order_manager.py` (or a dedicated `sim/` folder). It needs to walk the live reconstructed orderbook at the exact moment of our signal and apply realistic slippage to track PnL.

#### Task 7: Live Deployment

Gate live trading behind a verified paper trading edge. Add position limits and a max daily loss kill switch before ever flipping `EXECUTION_MODE=LIVE`.
