# Kalshi 15-minute crypto trader

Fee-aware systematic trading for Kalshi's single-asset 15-minute crypto
markets, with optional discretionary overrides.

## Quick start

```bash
uv sync
cp .env.example .env
uv run src/main.py bitcoin
```

Open [http://127.0.0.1:3000](http://127.0.0.1:3000), choose **Sim**, then click **Start**
or **Start Live**. The process starts stopped and neither destination has a
confirmation step.

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

The orderbook uses Kalshi's sequenced WebSocket snapshot and deltas. A sequence
gap pauses book consumers while the same subscription requests a fresh snapshot;
only a failed recovery reconnects the socket.

### Sim and live

**Sim** never calls Kalshi's order-entry API. It:

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

## Operating workflow

The engine always submits model entries and exits while it is running. The
dashboard's collapsed **Operator Controls** contains model/risk settings and a
click ticket for discretionary overrides.

The engine evaluates once per second. Risk-reducing exits can act every cycle;
new buys have a five-second default cooldown so account state can reconcile
before more risk is added.

Choose Buy or Sell/Reduce, YES or NO, and a contract count in **Click Order**.
These discretionary orders use the active market, top of book, configured
slippage, order and position limits, cash buffer, reduce-only checks, and
daily-loss guard. They do not disable the systematic engine.

### Stop and flatten

- **Stop** stops the engine. In live mode it also cancels this bot's resting
  orders.
- **Flatten Active Market** disarms, cancels, and submits a reduce-only IOC for
  the active position.
- A daily-loss breach disarms and attempts to flatten the active market. The
  lock persists until the next New York trading day.

## How the strategy decides

The model estimates the probability that Kalshi's final-minute settlement
average finishes at or above the rounded contract strike. When the opening
reference is available, the synthetic index is basis-anchored to it; a
mid-market start uses the live index unadjusted. Before buying, model value must
clear:

- the current marketable IOC price;
- the current Kalshi taker fee;
- configured slippage; and
- the configured minimum net edge.

Sizing is the smallest of several ceilings:

- fee-inclusive quarter-Kelly on current account equity;
- available cash after the cash buffer;
- max order contracts;
- max position dollars; and
- an edge ladder where every additional held contract requires another 0.5¢ of
  credit.

Entries also require a 30-second warm-up after each market change, an orderbook no more than two
seconds old, at least two clean index constituents, non-fallback volatility,
model probability between 5% and 95%, a known fee policy, and at least 20
seconds to expiry. Optional model/orderbook agreement can hard-gate entries.

Existing positions exit when the executable bid, after slippage and fees,
exceeds model fair value by the configured edge. Entry gates never block this
edge-reversal exit.

## Dashboard map

- **Autotrader strip:** sim/live mode, session P&L, start, stop, and flatten.
- **Positions:** cash, marked position value, open contracts, and realized P&L.
- **Market & Model:** index, model/market probability, edge, pricing, and
  realized-volatility fit.
- **Order book:** live YES/NO bids, asks, and depth.
- **High-Touch Trading:** model/risk parameters and manual orders.

Model probability, microstructure, cooldown, and the settlement-average line
reset at each 15-minute boundary. The rolling synthetic index and realized
volatility carry across markets; cash, daily P&L, risk locks, and settlement
tracking continue.

## Default controls

- Minimum net edge: 5¢ per contract
- Max order: 5 contracts
- Max position: $10 per outcome side
- Max daily equity loss: $10
- Cash buffer: $25
- Entry cooldown: 5 seconds
- Decision interval: 1 second
- IOC tolerance: 1 exchange tick

Settings are process-local. Trading events are appended to
`.runtime/execution_events.jsonl`; the paper and live daily-risk states are kept
separate under `.runtime/`.

## Tests

```bash
uv run --with pytest pytest -q
```
