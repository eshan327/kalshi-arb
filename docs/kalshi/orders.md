# Order integration — reviewed 2026-09-27

`data.kalshi_orders.OrderClient("predictions" | "perps")` provides explicit
single-order REST calls. Constructing it or calling `build_order` performs no
networking. `create_order`, `amend_order`, `decrease_order`, and `cancel_order`
submit a signed write when called. The feed CLIs and backtests never call them.

## Current API routes

| Operation | Predictions | Perps |
| --- | --- | --- |
| Create | `POST /portfolio/events/orders` | `POST /margin/orders` |
| Amend | `POST /portfolio/events/orders/{id}/amend` | `POST /margin/orders/{id}/amend` |
| Decrease | `POST /portfolio/events/orders/{id}/decrease` | `POST /margin/orders/{id}/decrease` |
| Cancel | `DELETE /portfolio/events/orders/{id}` | `DELETE /margin/orders/{id}` |
| Read one | `GET /portfolio/orders/{id}` | `GET /margin/orders/{id}` |
| List | `GET /portfolio/orders` | `GET /margin/orders` |

Paths are relative to `/trade-api/v2`. Predictions writes use the
[current V2 API](https://docs.kalshi.com/api-reference/orders/create-order-v2).
Its reads still use `/portfolio/orders`, not `/portfolio/events/orders`.
Perps follows its [current schema](https://docs.kalshi.com/margin-rest/orders/create-order).
Both use `side=bid|ask`, string `count`, dollar-string `price`, time-in-force,
and self-trade prevention. Write acknowledgements are compact objects; do not
expect an `order` wrapper or a full position snapshot.

## Preview and explicit submission

With `src` on the Python path, preview a Perps intent using current metadata:

```python
from uuid import uuid4
from data.kalshi_perps import get_perps
from data.kalshi_orders import OrderClient

client = OrderClient("perps")
market = get_perps("/markets/KXBTCPERP")["market"]
fields = dict(
    side="bid", count="1.00", price=market["bid"],
    client_order_id=str(uuid4()), time_in_force="immediate_or_cancel",
)
preview = client.build_order(market, **fields)
print(preview)

# Persist fields and the preview with the intent before deliberately submitting:
# acknowledgement = client.create_order(market, **fields)
```

For Predictions, construct `OrderClient("predictions")` and pass a market from
`get_open_markets(series_ticker)` or `GET /markets/{ticker}`. It must include
current `price_ranges`. The builder reads `exchange_index` from that market,
when available, unless you explicitly override it. Never derive the shard from
the ticker. Subaccount is omitted by default, including for locked API keys;
pass the intended 0–63 subaccount when needed.

[Prediction direction](https://docs.kalshi.com/getting_started/order_direction):
all V2 prices are on the Yes scale. Bid is long Yes; ask is long No/sell Yes.
For example, buying No for $0.42 is an ask at Yes-price $0.58. An ask already
quoted at $0.58 stays $0.58; this client never silently complements prices.

[Price grids](https://docs.kalshi.com/getting_started/fixed_point_migration) are
market-specific. Predictions validates against each `price_ranges` band;
Perps validates `tick_size` and `fractional_trading_enabled`. Numeric inputs
use strings, integers, or `Decimal`; floats, nonfinite values, excess decimal
precision, and off-grid prices are rejected rather than rounded. Counts have
0.01 precision, prices 0.0001 precision. Market metadata is supplied explicitly
so planning and validation do not hide an HTTP read. Refresh it before execution;
live balance, market state, Perps price bands, and risk checks remain exchange
constraints. No local validation guarantees acceptance or fills.

Time-in-force is an explicit caller choice. Defaults include self-trade
prevention `taker_at_cross` and `cancel_order_on_pause=True`. Expiration uses
Unix **seconds** with GTC. Reduce-only requires IOC on Predictions; Perps also
accepts FOK. Do not infer these REST constraints from the future FIX release.

## Management and uncertain outcomes

`amend_order(id, market, side=..., price=..., count=...)` takes **total/max
fillable count**, including previously filled quantity. Optional original and
updated client IDs are supported. Increasing quantity or changing price can
forfeit queue position; a size-only decrease preserves it per the docs.

`decrease_order(id, reduce_by=... | reduce_to=...)` requires exactly one selector.
`reduce_to="0.00"` is valid. For Predictions decrease/cancel, supply
`market_ticker=...` for automatic routing or an explicit `exchange_index`.
The client rejects an order-ID-only cancellation that would otherwise silently
fall back to shard 0. Cancellation routing lives in query parameters; decrease
routing lives in the JSON body, while subaccount lives in the query. Perps
cancel/decrease takes order ID and optional subaccount, without shard/ticker
routing fields. Amend uses subaccount in the query on both products.

Writes make **one attempt**, with no automatic retry or redirect following.
Timeouts, disconnects, server errors, and malformed success bodies raise
`OrderOutcomeUnknown`. This means the write may have been processed. Inspect
orders/fills before another mutation. Validation/API rejections (including 409
and 429) propagate as HTTP errors, retaining the response. Client IDs are
caller-supplied and must be generated/persisted once per intent; do not generate
a new ID merely because acknowledgement was lost.

`get_order(id)` and `get_orders(ticker=..., subaccount=..., cursor=...)` use the
existing authenticated GET retry transport. Lists return one page and its
opaque cursor. There is no direct `client_order_id` filter for these direct-member
list endpoints: page through the relevant ticker/time range and inspect the
returned client IDs. A missing match is not proof a write failed: reconcile with
private order/fill notifications and allow for the documented read lag.
Predictions archived canceled/executed orders require `/historical/orders`;
Perps has no corresponding historical endpoint.

The [create guide](https://docs.kalshi.com/getting_started/quick_start_create_order)
describes client-ID deduplication. Treat a duplicate rejection as a reconciliation
signal, not as proof the prior request filled. A successful create/amend also
can fill immediately; retain actual acknowledgement counts, fees, and timestamps.

[Rate limits](https://docs.kalshi.com/getting_started/rate_limits) differ by
product and operation. Writes are not run through the read retry loop. Handle
429 explicitly after examining the error, keeping the same intent identity.
The current single-call client adds no background executor, risk policy, position
ledger, private-feed consumer, write pacing, batch orders, mass cancellation,
order groups, or FIX engine. Add those when an actual execution workflow needs
them; automated trading also needs durable intent state and fill reconciliation.

Schemas and changelog were refreshed on September 27. The October 1 FIX and
user-order-field announcements remain future-dated. The implementation depends
on current REST behavior and preserves optional response fields.
