class OrderBook:
    """
    Maintains a live L2 orderbook for a single Kalshi market.

    Algorithm:
    1. Subscribe to WS — first message is orderbook_snapshot (seq=1)
    2. Load snapshot into yes/no dicts
    3. Apply orderbook_delta messages (seq=2, 3, ...)
    4. If seq gap detected: set needs_resync flag — streamer reconnects
    5. On reconnect: fresh snapshot from WS, seq resets to 1
    """

    def __init__(self, market_ticker):
        self.market_ticker = market_ticker
        self.yes = {}  # {price_dollars_str: quantity_float}
        self.no = {}
        self.expected_seq = None
        self.initialized = False
        self.needs_resync = False

    def load_ws_snapshot(self, msg):
        """
        Loads the WS orderbook_snapshot message.
        msg keys: yes_dollars_fp, no_dollars_fp, market_ticker, market_id
        """
        self.yes.clear()
        self.no.clear()

        for price_str, qty_str in msg.get("yes_dollars_fp", []):
            qty = float(qty_str)
            if qty > 0:
                self.yes[price_str] = qty

        for price_str, qty_str in msg.get("no_dollars_fp", []):
            qty = float(qty_str)
            if qty > 0:
                self.no[price_str] = qty

        self.initialized = True
        self.needs_resync = False
        print(f"  --> Snapshot loaded: {len(self.yes)} yes, {len(self.no)} no")

    def apply_delta(self, msg):
        """
        Applies a single WS orderbook_delta message.
        msg keys: price_dollars, delta_fp, side, ts
        delta_fp is the CHANGE in quantity (positive = add, negative = remove).
        """
        side_str = msg.get("side")
        price = msg.get("price_dollars")
        delta = float(msg.get("delta_fp", 0))

        if side_str == "yes":
            book = self.yes
        elif side_str == "no":
            book = self.no
        else:
            return

        new_qty = book.get(price, 0) + delta

        if new_qty <= 0:
            book.pop(price, None)
        else:
            book[price] = new_qty

    def check_seq(self, seq):
        """
        Checks if the sequence number is what we expect.
        Returns True if OK, False if there's a gap (needs resync).
        """
        if self.expected_seq is None:
            self.expected_seq = seq + 1
            return True

        if seq != self.expected_seq:
            print(f"  --> Seq gap detected: expected {self.expected_seq}, got {seq}")
            self.needs_resync = True
            return False

        self.expected_seq = seq + 1
        return True

    def reset(self):
        """Clears all state for a fresh reconnect."""
        self.yes.clear()
        self.no.clear()
        self.expected_seq = None
        self.initialized = False
        self.needs_resync = False

    def get_orderbook(self):
        """
        Returns the current orderbook as sorted lists in cents.
        yes_bids: [(price_cents, qty), ...] sorted descending
        yes_asks: implied from no_bids (100 - no_price)
        no_bids: [(price_cents, qty), ...] sorted descending
        no_asks: implied from yes_bids (100 - yes_price)
        """
        yes_bids = sorted(
            [(int(round(float(p) * 100)), q) for p, q in self.yes.items()],
            key=lambda x: x[0], reverse=True
        )
        no_bids = sorted(
            [(int(round(float(p) * 100)), q) for p, q in self.no.items()],
            key=lambda x: x[0], reverse=True
        )

        yes_asks = sorted([(100 - p, q) for p, q in no_bids])
        no_asks = sorted([(100 - p, q) for p, q in yes_bids])

        return yes_bids, yes_asks, no_bids, no_asks

    def get_best_prices(self):
        """Returns (yes_best_bid, yes_best_ask, no_best_bid, no_best_ask) in cents."""
        yes_bids, yes_asks, no_bids, no_asks = self.get_orderbook()
        return (
            yes_bids[0][0] if yes_bids else None,
            yes_asks[0][0] if yes_asks else None,
            no_bids[0][0] if no_bids else None,
            no_asks[0][0] if no_asks else None,
        )
