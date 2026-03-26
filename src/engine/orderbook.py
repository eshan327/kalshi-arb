class OrderBook:
    """
    Maintains a live L2 orderbook for a single Kalshi market.

    Algorithm:
    1. Fetch REST snapshot and load yes/no levels
    2. Buffer incoming WS orderbook_delta events while bootstrapping
    3. Initialize expected sequence using snapshot anchor
    4. Replay buffered deltas in-order and ignore stale seqs
    5. Apply live deltas continuously; reconnect on any seq gap
    """

    def __init__(self, market_ticker):
        self.market_ticker = market_ticker
        self.yes = {}  # {price_dollars_str: quantity_float}
        self.no = {}
        self.expected_seq = None
        self.initialized = False
        self.needs_resync = False
        self.qty_epsilon = 1e-6

    def _normalize_qty(self, qty_value):
        """Normalizes quantities and strips near-zero float residue."""
        qty = round(float(qty_value), 2)
        if abs(qty) < self.qty_epsilon:
            return 0.0
        return qty

    @staticmethod
    def _to_cents(price_dollars_str):
        """Converts dollars string to cents with decimal precision preserved."""
        return round(float(price_dollars_str) * 100.0, 2)

    @staticmethod
    def _normalize_price(price_value):
        """Normalizes REST/WS prices to dollars string format (e.g. '0.5100')."""
        value = float(price_value)
        if value > 1:
            value = value / 100.0
        return f"{value:.4f}"

    @staticmethod
    def _extract_seq(snapshot_msg):
        """Extracts sequence from a snapshot payload if present."""
        for key in ("seq", "sequence"):
            seq = snapshot_msg.get(key)
            if isinstance(seq, int):
                return seq
        return None

    def _load_levels(self, levels, destination):
        """Loads [price, qty] levels into a destination side dict."""
        destination.clear()
        for level in levels:
            if not isinstance(level, (list, tuple)) or len(level) < 2:
                continue
            price_raw, qty_raw = level[0], level[1]
            qty = self._normalize_qty(qty_raw)
            if qty <= 0:
                continue
            destination[self._normalize_price(price_raw)] = qty

    def load_ws_snapshot(self, msg):
        """
        Loads the WS orderbook_snapshot message.
        msg keys: yes_dollars_fp, no_dollars_fp, market_ticker, market_id
        """
        self._load_levels(msg.get("yes_dollars_fp", []), self.yes)
        self._load_levels(msg.get("no_dollars_fp", []), self.no)

        seq = self._extract_seq(msg)
        if isinstance(seq, int):
            self.expected_seq = seq + 1

        self.initialized = True
        self.needs_resync = False
        print(f"  --> Snapshot loaded: {len(self.yes)} yes, {len(self.no)} no")

    def load_rest_snapshot(self, snapshot):
        """
        Loads REST snapshot payload into yes/no books.
        Accepts both {yes,no} and {yes_dollars_fp,no_dollars_fp} structures.
        Returns snapshot sequence if available, else None.
        """
        yes_levels = snapshot.get("yes")
        no_levels = snapshot.get("no")

        if yes_levels is None or no_levels is None:
            yes_levels = snapshot.get("yes_dollars_fp", [])
            no_levels = snapshot.get("no_dollars_fp", [])

        self._load_levels(yes_levels or [], self.yes)
        self._load_levels(no_levels or [], self.no)

        self.initialized = True
        self.needs_resync = False

        seq = self._extract_seq(snapshot)
        print(
            f"  --> REST snapshot loaded: {len(self.yes)} yes, {len(self.no)} no"
            + (f" | seq={seq}" if seq is not None else " | seq=n/a")
        )
        return seq

    def set_expected_seq(self, expected_seq):
        """Sets the next expected sequence id."""
        if isinstance(expected_seq, int):
            self.expected_seq = expected_seq

    def apply_delta(self, msg):
        """
        Applies a single WS orderbook_delta message.
        msg keys: price_dollars, delta_fp, side, ts
        delta_fp is the CHANGE in quantity (positive = add, negative = remove).
        """
        side_str = msg.get("side")
        price = self._normalize_price(msg.get("price_dollars"))
        delta = self._normalize_qty(msg.get("delta_fp", 0))

        if side_str == "yes":
            book = self.yes
        elif side_str == "no":
            book = self.no
        else:
            return

        new_qty = self._normalize_qty(book.get(price, 0.0) + delta)

        if new_qty <= 0:
            book.pop(price, None)
        else:
            book[price] = new_qty

    def check_seq(self, seq):
        """
        Checks if the sequence number is what we expect.
        Returns True if OK, False if there's a gap (needs resync).
        """
        if not isinstance(seq, int):
            print(f"  --> Invalid seq value: {seq}")
            self.needs_resync = True
            return False

        if self.expected_seq is None:
            self.expected_seq = seq + 1
            return True

        if seq != self.expected_seq:
            print(f"  --> Seq gap detected: expected {self.expected_seq}, got {seq}")
            self.needs_resync = True
            return False

        self.expected_seq = seq + 1
        return True

    def apply_delta_with_seq(self, seq, msg):
        """
        Applies a delta only when sequence is in-order.
        Returns True when applied, False when stale/invalid/gap.
        """
        if not isinstance(seq, int):
            self.needs_resync = True
            return False

        if self.expected_seq is None:
            self.expected_seq = seq

        if seq < self.expected_seq:
            return False

        if seq > self.expected_seq:
            print(f"  --> Seq gap detected: expected {self.expected_seq}, got {seq}")
            self.needs_resync = True
            return False

        self.apply_delta(msg)
        self.expected_seq = seq + 1
        return True

    def apply_buffered_deltas(self, buffered_deltas):
        """Replays buffered deltas in ascending sequence order."""
        applied = 0
        for seq, msg in sorted(buffered_deltas, key=lambda x: x[0]):
            if self.apply_delta_with_seq(seq, msg):
                applied += 1
            if self.needs_resync:
                break
        return applied

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
            [(self._to_cents(p), self._normalize_qty(q)) for p, q in self.yes.items()],
            key=lambda x: x[0], reverse=True
        )
        no_bids = sorted(
            [(self._to_cents(p), self._normalize_qty(q)) for p, q in self.no.items()],
            key=lambda x: x[0], reverse=True
        )

        yes_asks = sorted([(round(100.0 - p, 2), q) for p, q in no_bids])
        no_asks = sorted([(round(100.0 - p, 2), q) for p, q in yes_bids])

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
