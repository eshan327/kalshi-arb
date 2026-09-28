import logging
import math
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
        self.yes = {}  # {price_cents: quantity}
        self.no = {}
        self.expected_seq = None
        self.initialized = False
        self.needs_resync = False
        self._lock = RLock()

    def _normalize_qty(self, qty_value):
        """Normalizes quantities and strips near-zero float residue."""
        qty = round(float(qty_value), 2)
        if not math.isfinite(qty):
            raise ValueError("Nonfinite quantity")
        return qty

    @staticmethod
    def _normalize_price(price_value):
        """Convert WebSocket dollar prices to cents."""
        value = float(price_value)
        if not math.isfinite(value):
            raise ValueError("Nonfinite price")
        if value <= 0:
            return None

        cents = value * 100
        if cents >= 100:
            raise ValueError("Price out of range")
        return round(cents, 4)

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

    def _is_crossed_unlocked(self):
        return bool(self.yes and self.no and max(self.yes) + max(self.no) >= 100.0)

    def load_ws_snapshot(self, snapshot, seq):
        """Load YES and NO bids at their own prices and the exact sequence."""
        with self._lock:
            self._load_levels(
                snapshot.get("yes_dollars_fp", []),
                self.yes,
            )
            self._load_levels(
                snapshot.get("no_dollars_fp", []),
                self.no,
            )
            self.expected_seq = seq + 1 if isinstance(seq, int) else None
            self.initialized = True
            self.needs_resync = self._is_crossed_unlocked()

    def apply_delta(self, msg):
        """
        Applies a single WS orderbook_delta message.
        msg keys: price_dollars, delta_fp, side, ts. WebSocket prices use the
        price of the indicated YES or NO bid.
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

            if new_qty < 0:
                self.needs_resync = True
                return
            if new_qty == 0:
                book.pop(price, None)
            else:
                book[price] = new_qty
            self.needs_resync = self.needs_resync or self._is_crossed_unlocked()

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

    def get_best_prices(self):
        """Returns (yes_best_bid, yes_best_ask, no_best_bid, no_best_ask) in cents."""
        with self._lock:
            if self.needs_resync or self._is_crossed_unlocked():
                return None, None, None, None
            yes_bid = max(self.yes, default=None)
            no_bid = max(self.no, default=None)
            return (
                yes_bid,
                round(100.0 - no_bid, 4) if no_bid is not None else None,
                no_bid,
                round(100.0 - yes_bid, 4) if yes_bid is not None else None,
            )
