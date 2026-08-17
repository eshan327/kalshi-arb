# Kalshi 15-minute crypto trader

Fee-aware systematic, semi-systematic, and click trading for Kalshi's
single-asset 15-minute crypto markets.

## Quick start

```bash
uv sync
cp .env.example .env
uv run src/main.py bitcoin
```

Open [http://127.0.0.1:5000](http://127.0.0.1:5000), choose a trading style,
then click **Start Paper** or **Start Live**. The process starts stopped and
neither destination has a confirmation step.

Supported assets: `BTC`, `ETH`, `SOL`, `XRP`, `DOGE`, `BNB`, `ADA`, `NEAR`,
`BCH`, `HYPE`, `TON`, and `ZEC`. Names such as `bitcoin` and `solana` also work.

```bash
uv run src/main.py ETH
uv run src/main.py SOL
```

If the asset is omitted, `KALSHI_MARKET_ASSET` is used, then BTC.

## Credentials and modes

Both modes need a Kalshi API key because the live orderbook WebSocket is
authenticated. Configure the demo or production key selected by `KALSHI_ENV` in
`.env`.

### Paper and live

**Start Paper** never calls Kalshi's order-entry API. It:

- starts with an ephemeral $1,000 balance by default;
- simulates IOC fills against displayed live top-of-book price and quantity;
- applies the active Kalshi fee multiplier;
- marks open positions to the live bid;
- supports automated exits, discretionary orders, pause, and flatten; and
- waits for Kalshi's finalized outcome before settling expired positions.

Set `KALSHI_PAPER_STARTING_CASH_CENTS` to change the starting balance. The paper
account resets when the process restarts.

**Start Live** sends IOC orders to the Kalshi environment selected by
`KALSHI_ENV`: `demo` uses Kalshi demo and `prod` uses production. **Stop**
disables order entry. Switching from live to paper also cancels this bot's
resting live orders.

## Operating workflows

There is one dashboard. The trading-style selector changes who submits strategy
orders; it does not hide tools or move the operator into another GUI.

### Systematic

Select **Systematic — auto in/out**. The engine submits both model entries and
model exits. The click ticket remains available for discretionary overrides.

The engine evaluates once per second. Risk-reducing exits can act every cycle;
new buys have a five-second default cooldown so account state can reconcile
before more risk is added.

### Semi-systematic

Select **Semi — click in, auto out**. You enter positions from **Click Order**;
the engine continues to manage model exits.

### Click trading

Select **Click trading — manual in/out**. The engine displays its model but does
not submit model entries or exits. Choose Buy or Sell/Reduce, YES or NO, and a
contract count in **Click Order**, then submit.

Click orders use the active market, top of book, configured slippage, order and
position limits, cash buffer, reduce-only checks, and daily-loss guard.

### Stop and flatten

- **Stop** stops the engine. In live mode it also cancels this bot's resting
  orders.
- **Flatten Active Market** disarms, cancels, and submits a reduce-only IOC for
  the active position.
- A daily-loss breach disarms and attempts to flatten the active market. The
  lock persists until the next New York trading day.

## How the strategy decides

The model estimates the probability that Kalshi's final-minute settlement
average finishes above the contract strike. Before buying, model value must
clear:

- the current marketable IOC price;
- the current Kalshi taker fee;
- configured slippage; and
- the configured minimum net edge.

Sizing is the smallest of several ceilings:

- time-weighted quarter-Kelly on current account equity;
- available cash after the cash buffer;
- max order contracts;
- max position dollars; and
- an edge ladder where every additional held contract requires another 0.5¢ of
  credit.

Entries also require a 30-second data warm-up, a fresh orderbook, non-fallback
volatility, model probability between 20% and 80%, a known fee policy, and at
least ten seconds to expiry. Optional model/orderbook agreement can hard-gate
entries.

Existing positions can exit on probability guardrails, profit plus edge decay,
max-position edge decay, or edge reversal. Entry gates never block these exits.

## Dashboard map

- **Operator strip:** trading style, paper/live start, running state, active
  market, data freshness, daily P&L/lock, stop, and flatten.
- **Systematic Policy:** entry, risk, sizing, slippage, volatility, and
  model/orderbook agreement settings.
- **Click Order:** manual buy/reduce ticket for the active market.
- **Account and Signal:** current intent, fair value, implied probability, edge,
  cash, equity, and positions.
- **Market, Model & Orderbook:** live YES/NO depth, synthetic index, settlement
  proxy, probability model, and microstructure signal.
- **Diagnostics & Audit:** reconciliation, raw streams, and calculation details;
  these are intentionally secondary to the operator controls.

## Default controls

- Minimum net edge: 3¢ per contract
- Max order: 5 contracts
- Max position: $10 per outcome side
- Max daily equity loss: $10
- Cash buffer: $25
- Entry cooldown: 5 seconds
- Decision interval: 1 second
- IOC slippage: 1 exchange tick

Settings are process-local. Trading events are appended to
`.runtime/execution_events.jsonl`; the paper and live daily-risk states are kept
separate under `.runtime/`.

## API

- `GET /api/state`
- `GET|POST /api/settings`
- `GET /api/trading/runtime`
- `GET /api/trading/events`
- `POST /api/trading/control`
- `POST /api/trading/manual`

## Tests

```bash
uv run --with pytest pytest -q
```
