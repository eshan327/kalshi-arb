# Kalshi 15-minute crypto trader

Fee-aware systematic and discretionary trading for Kalshi's single-asset
15-minute crypto markets. One process trades one asset in either paper or live
mode.

## Quick start

```bash
uv sync
cp .env.example .env
uv run src/main.py --paper bitcoin
```

Open [http://127.0.0.1:5000](http://127.0.0.1:5000), verify the status strip,
review the default limits, then click **Arm Paper** and enter `ARM PAPER`.

Supported assets: `BTC`, `ETH`, `SOL`, `XRP`, `DOGE`, `BNB`, `ADA`, `NEAR`,
`BCH`, `HYPE`, `TON`, and `ZEC`. Names such as `bitcoin` and `solana` also work.

```bash
uv run src/main.py --paper ETH
uv run src/main.py --live SOL
```

If the mode is omitted, paper is the safe default. If the asset is omitted,
`KALSHI_MARKET_ASSET` is used, then BTC.

## Credentials and modes

Both modes need a Kalshi API key because the live orderbook WebSocket is
authenticated. Configure the demo or production key selected by `KALSHI_ENV` in
`.env`.

### Paper

`--paper` never calls Kalshi's order-entry API. It:

- starts with an ephemeral $1,000 balance by default;
- simulates IOC fills against displayed live top-of-book price and quantity;
- applies the active Kalshi fee multiplier;
- marks open positions to the live bid;
- supports automated exits, discretionary orders, pause, and flatten; and
- waits for Kalshi's finalized outcome before settling expired positions.

Set `KALSHI_PAPER_STARTING_CASH_CENTS` to change the starting balance. The paper
account resets when the process restarts.

### Live

`--live` sends IOC orders to the Kalshi environment selected by `KALSHI_ENV`.
It remains unable to submit orders until this explicit gate is also set:

```env
KALSHI_LIVE_TRADING_ENABLED=true
```

`KALSHI_ENV=demo` sends orders to Kalshi demo. `KALSHI_ENV=prod` sends real-money
orders. Every process starts disarmed and arming never survives a restart.

## Operating workflows

### Fully systematic

1. Start in paper mode.
2. Confirm the status strip shows the intended mode, market, fresh data, and no
   daily-loss lock.
3. Review **Systematic Policy** and click **Save Settings**.
4. Leave **New Systematic Entries** enabled.
5. Arm the process with the displayed confirmation.
6. Monitor signal intent, model fair value, current position, equity, and daily
   P&L.

The engine evaluates once per second. Risk-reducing exits can act every cycle;
new buys have a five-second default cooldown so account state can reconcile
before more risk is added.

### Semi-systematic

Leave systematic entries enabled and use **Discretionary IOC** only when you
want to add or reduce a specific side. Automated position exits remain active.
The discretionary ticket bypasses the model edge and Kelly decision, but it
does not bypass arming, the daily-loss guard, cash buffer, order cap, position
cap, slippage setting, or reduce-only sell checks.

### Manual-only

1. Set **New Systematic Entries** to **Disabled** and save settings.
2. Arm the process.
3. Choose Buy or Sell/Reduce, YES or NO, and contract count in
   **Discretionary IOC**.
4. Review the displayed top quote, model fair value, held quantity, and IOC
   protection.
5. Submit and enter `SUBMIT PAPER` or `SUBMIT LIVE` exactly.

Manual-only mode still retains the account-wide daily-loss guard. Automated
signal exits are also still evaluated for existing positions; use **Pause** if
you want all strategy submissions stopped.

### Stop and flatten

- **Pause** disarms the engine. In live mode it also cancels this bot's resting
  orders.
- **Flatten Active Market** disarms, cancels, and submits a reduce-only IOC for
  the active position. It requires `FLATTEN`.
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
least ten seconds to expiry. Optional orderbook-probability confirmation can
hard-gate entries.

Existing positions can exit on probability guardrails, profit plus edge decay,
max-position edge decay, or edge reversal. Entry gates never block these exits.

## Dashboard map

- **Operator strip:** mode, armed state, active market, decision-loop freshness,
  daily P&L/lock, arm, pause, and flatten.
- **Systematic Policy:** entry, risk, sizing, slippage, volatility, and
  orderbook-confirmation settings.
- **Discretionary IOC:** safe manual buy/reduce ticket for the active market.
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
