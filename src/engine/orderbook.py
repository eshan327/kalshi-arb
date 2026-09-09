import heapq
import logging
import math
import time
from threading import RLock

logger = logging.getLogger(__name__)


class OrderBook:
    """
    Maintains a live L2 orderbook for a single Kalshi market.

    Algorithm:
    1. Load the sequence-aligned WebSocket snapshot
    2. Replay buffered deltas in-order and ignore stale seqs
    3. Apply live deltas continuously; reconnect on any gap or crossed book
    """

    def __init__(self, market_ticker):
        self.market_ticker = market_ticker
        self.yes = {}  # {price_cents_int: quantity_float}
        self.no = {}
        self.expected_seq = None
        self.initialized = False
        self.needs_resync = False
        self.last_update_ts = None
        self.last_verified_ts = None
        self.qty_epsilon = 1e-6
        self._lock = RLock()

    def _normalize_qty(self, qty_value):
        """Normalizes quantities and strips near-zero float residue."""
        qty = round(float(qty_value), 2)
        if not math.isfinite(qty):
            raise ValueError("Nonfinite quantity")
        if abs(qty) < self.qty_epsilon:
            return 0.0
        return qty

    @staticmethod
    def _normalize_price(price_value, *, dollars=None):
        """Normalizes REST/WS prices to fixed-point cents."""
        value = float(price_value)
        if not math.isfinite(value):
            raise ValueError("Nonfinite price")
        if value <= 0:
            return None

        cents = (
            value * 100.0
            if dollars is True or (dollars is None and value <= 1)
            else value
        )
        if cents >= 100:
            raise ValueError("Price out of range")
        return round(cents, 4)

    @staticmethod
    def _extract_seq(snapshot_msg):
        """Extracts sequence from a snapshot payload if present."""
        for key in ("seq", "sequence"):
            seq = snapshot_msg.get(key)
            if isinstance(seq, int):
                return seq
        return None

    def _load_levels(self, levels, destination, *, dollars, invert_price=False):
        """Loads [price, qty] levels into a destination side dict."""
        destination.clear()
        for level in levels:
            if not isinstance(level, (list, tuple)) or len(level) < 2:
                continue
            price_raw, qty_raw = level[0], level[1]
            qty = self._normalize_qty(qty_raw)
            price_cents = self._normalize_price(price_raw, dollars=dollars)
            if qty <= 0:
                continue
            if price_cents is None:
                continue
            if invert_price:
                price_cents = round(100.0 - price_cents, 4)
            destination[price_cents] = qty

    def _is_crossed_unlocked(self):
        return bool(self.yes and self.no and max(self.yes) + max(self.no) >= 100.0)

    def _top_n_levels(self, side_book, depth):
        """Returns top-N descending bid levels from an internal side map."""
        if depth <= 0 or not side_book:
            return []

        # nlargest avoids sorting the full book.
        top_items = heapq.nlargest(depth, side_book.items(), key=lambda item: item[0])
        return [
            (float(price_cents), self._normalize_qty(qty))
            for price_cents, qty in top_items
        ]

    def load_rest_snapshot(self, snapshot):
        """
        Loads REST snapshot payload into yes/no books.
        Accepts both {yes,no} and {yes_dollars_fp,no_dollars_fp} structures.
        Returns snapshot sequence if available, else None.
        """
        with self._lock:
            yes_levels = snapshot.get("yes")
            no_levels = snapshot.get("no")

            dollars = yes_levels is None or no_levels is None
            if dollars:
                yes_levels = snapshot.get("yes_dollars_fp", [])
                no_levels = snapshot.get("no_dollars_fp", [])

            self._load_levels(yes_levels or [], self.yes, dollars=dollars)
            self._load_levels(no_levels or [], self.no, dollars=dollars)

            self.initialized = True
            self.needs_resync = self._is_crossed_unlocked()
            self.last_update_ts = time.time()

            seq = self._extract_seq(snapshot)
            logger.info(
                "REST snapshot loaded: %s yes, %s no | seq=%s",
                len(self.yes),
                len(self.no),
                seq if seq is not None else "n/a",
            )
            return seq

    def load_ws_snapshot(self, snapshot, seq):
        """Load a unified-YES-price WebSocket snapshot at its exact sequence."""
        with self._lock:
            self._load_levels(
                snapshot.get("yes_dollars_fp", []),
                self.yes,
                dollars=True,
            )
            self._load_levels(
                snapshot.get("no_dollars_fp", []),
                self.no,
                dollars=True,
                invert_price=True,
            )
            self.expected_seq = seq + 1 if isinstance(seq, int) else None
            self.initialized = True
            self.needs_resync = self._is_crossed_unlocked()
            self.last_update_ts = time.time()

    def apply_delta(self, msg):
        """
        Applies a single WS orderbook_delta message.
        msg keys: price_dollars, delta_fp, side, ts. WebSocket prices use the
        unified YES scale and are converted to the internal YES/NO leg scales.
        delta_fp is the CHANGE in quantity (positive = add, negative = remove).
        """
        with self._lock:
            side_str = msg.get("side")
            price = self._normalize_price(msg.get("price_dollars"), dollars=True)
            delta = self._normalize_qty(msg.get("delta_fp", 0))

            if price is None:
                return

            if side_str == "yes":
                book = self.yes
            elif side_str == "no":
                book = self.no
                price = round(100.0 - price, 4)
            else:
                return

            new_qty = self._normalize_qty(book.get(price, 0.0) + delta)

            if new_qty <= 0:
                book.pop(price, None)
            else:
                book[price] = new_qty
            self.needs_resync = self.needs_resync or self._is_crossed_unlocked()
            self.last_update_ts = time.time()

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
                logger.warning(
                    "Seq gap detected: expected %s, got %s", self.expected_seq, seq
                )
                self.needs_resync = True
                return False

            self.apply_delta(msg)
            self.expected_seq = seq + 1
            return True

    def reset(self):
        """Clears all state for a fresh reconnect."""
        with self._lock:
            self.yes.clear()
            self.no.clear()
            self.expected_seq = None
            self.initialized = False
            self.needs_resync = False
            self.last_update_ts = None
            self.last_verified_ts = None

    def get_orderbook_top_n(self, depth):
        """Returns top-N slices of the current orderbook in cents for low-latency read paths."""
        with self._lock:
            depth = max(0, int(depth))
            if self.needs_resync or self._is_crossed_unlocked():
                return [], [], [], []
            yes_bids = self._top_n_levels(self.yes, depth)
            no_bids = self._top_n_levels(self.no, depth)

            yes_asks = sorted([(round(100.0 - p, 4), q) for p, q in no_bids])
            no_asks = sorted([(round(100.0 - p, 4), q) for p, q in yes_bids])
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
