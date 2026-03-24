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
- `config.py` and `auth.py` are for environment variables and authentication
- `display.py` handles terminal formatting right now (rework to visualize the live orderbook later)

**Data Infrastructure (`src/data/`)**
- `kalshi_rest.py` pulls static JSON snapshots from market
- `kalshi_ws.py` keeps the WebSocket tunnel open
- `orderbook_math.py` handles pure math functions (e.g., implied asks)

**Engine & Math (`src/engine/`)**
- `streamer.py` is an aysnc event loop routing WS traffic
- `twap.py` tracks the rolling window, elapsed time, and required remaining average

**Execution (`src/execution/`)**
- `order_manager.py` routes orders and has safety valves (QT button mashing simulator)

---

### What's Left

Right now, we are catching data but the bot doesn't "see" the market state. Here is the build order. *Tasks 1 and 2 can be done in parallel.* **This plan is tentative and speculative; modify as needed.**

#### Task 1: Orderbook Reconstruction

We can't trade on isolated WebSocket messages. We need to build `src/engine/orderbook.py`. It needs to:

1. Fetch a REST snapshot on startup.
2. Buffer incoming WS delta events from `streamer.py`.
3. Filter stale sequence IDs and apply deltas to the snapshot in order.
4. Expose a clean `get_orderbook()` interface so our math engine can read the live state.

#### Task 2: BRTI Proxy

Kalshi settles on the CF Benchmarks BRTI, and we need to synthesize the BRTI without direct API access.

1. Create `src/feeds/` directory.
2. Build WS connections to Coinbase, Kraken, and Gemini.
3. Build `brti_aggregator.py` to collect trades in a rolling window and output a volume-weighted median price.

#### Task 3: Asian Options Pricer & Volatility

We have the TWAP tracker but we need to price the uncertainty of the remaining time.

1. Build `asian_pricer.py`. We will use a model like the Levy approximation to calculate the lognormal probability distribution of the remaining 60-second average.
2. Build `vol_estimator.py` to calculate realized vol from our BRTI proxy to feed into the pricer.

#### Task 4: OBP Signal

Build `src/engine/obp.py` to quantify directional conviction. Calculate bid/ask imbalance at the top 5/10/etc levels and the size-weighted mid-price. This tells us if human traders are spoofing or actually moving a certain way.

#### Task 5: Signal Combiner

Build `src/engine/signal.py`. Merge the fair value from the Asian pricer with the conviction from the OBP. Calculate the edge minus taker fees. If `edge > min_edge` and `obp_aligned == True`, emit a `TradeSignal`.

#### Task 6: Paper Trading Simulator

Kalshi's demo markets are illiquid and useless for high-frequency testing. We need a local sim built into `order_manager.py` (or a dedicated `sim/` folder). It needs to walk the live reconstructed orderbook at the exact moment of our signal and apply realistic slippage to track PnL.

#### Task 7: Live Deployment

Gate live trading behind a verified paper trading edge. Add position limits and a max daily loss kill switch before ever flipping `EXECUTION_MODE=LIVE`.