# kalshi-arb

---

## Setup

### Package Management
- Use `uv` as your Python package manager (download if you don't have it)
    - `pyproject.toml` contains all project dependencies
    - Running `uv sync` will give you the needed dependencies ez
    - Use `uv add [name]` if you need a new package (will update the .toml)

### Environment Variables
Your `.env` file should look like this:
```
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
- Just nickname them prod and demo so your files are `prod.txt` and `demo.txt`
- Put the .txt files under a gitignored folder called `.secrets`
- The `KEY_ID` is copypasteable from Kalshi settings, put them in the `.env`
- Pls make sure everything is gitignored properly or I lowk steal your bank account

---

## Code Guidelines

### Structure
- Everything that matters is under the `src` directory 
- We're modularizing everything into subdirectories for a reason, it's more maintainable and organized
- Seperate & simplify components as much as you can, market-making project bloated to like a 1500-line `main.py` it was cooked

### Best Practices
- It's best to leave brief comments under both functions & important code blocks so everyone understands your code and knows what does what (important for debugging)
- LLMs are a second resort to reading docs. It's obv useful when on a leash but will bloat the codebase into a mess without clear guidance
- Don't let tech debt accumulate. Read this: https://www.ibm.com/think/topics/technical-debt
- Try to make small, iterative code changes and review/cleanup every change you make before continuing

### Principles
Follow DRY (don't repeat yourself), SOLID (most important part is Single Responsibility), KISS (Keep It Short and Simple) principles, and the MVC pattern

---

## Roadmap

### What's Built

**Authentication & Config**
- `config.py` and `auth.py` are the source for environment variables and cryptographic signing to handle authentication

**Data Infrastructure**
- `kalshi_rest.py` and `kalshi_ws.py` cover pulling snapshots (REST API) and the high-frequency streaming (WebSocket)
- `streamer.py` and `main.py` form an asynchronous event loop to handle continuous data
- `display.py` currently unused, will matter to reconstruct orderbook (`orderbook_math.py` supports this)

**Execution**
- `order_manager.py` is a functional order routing system with safety valves (e.g. QT button mashing)

**Other**
- `scanner.py` is probably not needed; leave it be for now to be safe

### What's Left

Right now we're basically catching data without really understanding the market data. To execute the settlement strat we need to transition to actively calculating.

**Task 1: Orderbook Reconstruction** (high priority)

We can't trade on isolated WebSocket messages (e.g., "someone canceled 5 contracts at 10¢"). We need to know the whole state. We have to code an algorithm that fetches a REST snapshot of the orderbook, buffers incoming WebSocket events, filters out old sequence IDs, and updates the snapshot. This creates an actually accurate orderbook.

**Task 2: CF Benchmarks Data Ingestion**

Kalshi Bitcoin markets settle on the CF Benchmarks Bitcoin Real Time Index (BRTI). Because settlement arb relies on knowing the exact numbers going into the final calculation, we must build a second, concurrent WebSocket connection to the CF Benchmarks API to stream the live spot price of Bitcoin alongside the Kalshi orderbook.

**Task 3: TWAP Arbitrage Engine** (alpha)

We need to maintain a rolling Time-Weighted Average Price of the BRTI over the final 60 seconds of the contract. The engine will constantly compare this running average against Kalshi's implied probabilities to mathematically flag when the market outcome becomes (at least near) deterministic. We'll need to look into mathematical pricing models.

**Task 4: Paper Trading Simulator**

Kalshi's demo environment is very flawed so we need our own execution sim. Running this should allow us to stream live production data and simulate our fills against the real orderbook slippage.

**Task 5: Live Deployment**

If/when we prove that we have a low-latency replication of the BRTI and that the TWAP strategy is profitable and accounts, we can try live trading by connecting engine signals to the order manager.