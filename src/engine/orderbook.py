import logging
import heapq
from threading import RLock


logger = logging.getLogger(__name__)


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
        self.yes = {}  # {price_cents_int: quantity_float}
        self.no = {}
        self.expected_seq = None
        self.initialized = False
        self.needs_resync = False
        self.qty_epsilon = 1e-6
        self._lock = RLock()

    def _normalize_qty(self, qty_value):
        """Normalizes quantities and strips near-zero float residue."""
        qty = round(float(qty_value), 2)
        if abs(qty) < self.qty_epsilon:
            return 0.0
        return qty

    @staticmethod
    def _to_cents(price_dollars_str):
        """Converts REST/WS prices to cents (supports dollars or cents input)."""
        value = float(price_dollars_str)
        if value <= 1:
            return round(value * 100.0, 2)
        return round(value, 2)

    @staticmethod
    def _normalize_price(price_value):
        """Normalizes REST/WS prices to integer cents."""
        value = float(price_value)
        if value <= 0:
            return None

        cents = value * 100.0 if value <= 1 else value
        return int(round(cents))

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
            price_cents = self._normalize_price(price_raw)
            if qty <= 0:
                continue
            if price_cents is None:
                continue
            destination[price_cents] = qty

    def _top_n_levels(self, side_book, depth):
        """Returns top-N descending bid levels from an internal side map."""
        if depth <= 0 or not side_book:
            return []

        # Internal keys are integer cents; nlargest avoids sorting the full book.
        top_items = heapq.nlargest(depth, side_book.items(), key=lambda item: item[0])
        return [(float(price_cents), self._normalize_qty(qty)) for price_cents, qty in top_items]

    def load_ws_snapshot(self, msg):
        """
        Loads the WS orderbook_snapshot message.
        msg keys: yes_dollars_fp, no_dollars_fp, market_ticker, market_id
        """
        with self._lock:
            self._load_levels(msg.get("yes_dollars_fp", []), self.yes)
            self._load_levels(msg.get("no_dollars_fp", []), self.no)

            seq = self._extract_seq(msg)
            if isinstance(seq, int):
                self.expected_seq = seq + 1

            self.initialized = True
            self.needs_resync = False
            logger.info("Snapshot loaded: %s yes, %s no", len(self.yes), len(self.no))

    def load_rest_snapshot(self, snapshot):
        """
        Loads REST snapshot payload into yes/no books.
        Accepts both {yes,no} and {yes_dollars_fp,no_dollars_fp} structures.
        Returns snapshot sequence if available, else None.
        """
        with self._lock:
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
            logger.info(
                "REST snapshot loaded: %s yes, %s no | seq=%s",
                len(self.yes),
                len(self.no),
                seq if seq is not None else "n/a",
            )
            return seq

    def set_expected_seq(self, expected_seq):
        """Sets the next expected sequence id."""
        with self._lock:
            if isinstance(expected_seq, int):
                self.expected_seq = expected_seq

    def apply_delta(self, msg):
        """
        Applies a single WS orderbook_delta message.
        msg keys: price_dollars, delta_fp, side, ts
        delta_fp is the CHANGE in quantity (positive = add, negative = remove).
        """
        with self._lock:
            side_str = msg.get("side")
            price = self._normalize_price(msg.get("price_dollars"))
            delta = self._normalize_qty(msg.get("delta_fp", 0))

            if price is None:
                return

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
        with self._lock:
            if not isinstance(seq, int):
                logger.warning("Invalid seq value: %s", seq)
                self.needs_resync = True
                return False

            if self.expected_seq is None:
                self.expected_seq = seq + 1
                return True

            if seq != self.expected_seq:
                logger.warning("Seq gap detected: expected %s, got %s", self.expected_seq, seq)
                self.needs_resync = True
                return False

            self.expected_seq = seq + 1
            return True

    def apply_delta_with_seq(self, seq, msg):
        """
        Applies a delta only when sequence is in-order.
        Returns True when applied, False when stale/invalid/gap.
        """
        with self._lock:
            if not isinstance(seq, int):
                self.needs_resync = True
                return False

            if self.expected_seq is None:
                self.expected_seq = seq

            if seq < self.expected_seq:
                return False

            if seq > self.expected_seq:
                logger.warning("Seq gap detected: expected %s, got %s", self.expected_seq, seq)
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
        with self._lock:
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
        with self._lock:
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

    def get_orderbook_top_n(self, depth):
        """Returns top-N slices of the current orderbook in cents for low-latency read paths."""
        with self._lock:
            depth = max(0, int(depth))
            yes_bids = self._top_n_levels(self.yes, depth)
            no_bids = self._top_n_levels(self.no, depth)

            yes_asks = sorted([(round(100.0 - p, 2), q) for p, q in no_bids])
            no_asks = sorted([(round(100.0 - p, 2), q) for p, q in yes_bids])
            return yes_bids, yes_asks, no_bids, no_asks

    def get_best_prices(self):
        """Returns (yes_best_bid, yes_best_ask, no_best_bid, no_best_ask) in cents."""
        yes_bids, yes_asks, no_bids, no_asks = self.get_orderbook_top_n(1)
        return (
            yes_bids[0][0] if yes_bids else None,
            yes_asks[0][0] if yes_asks else None,
            no_bids[0][0] if no_bids else None,
            no_asks[0][0] if no_asks else None,
        )
