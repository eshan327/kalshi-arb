"""Opt-in raw public feed capture and sequence-checked quote replay."""

from __future__ import annotations

import json
import time
from pathlib import Path

from data.orderbook import OrderBook


class FeedCapture:
    def __init__(self, path: str | None, session: str):
        self.session = session
        self.handle = None
        if path:
            target = Path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            self.handle = target.open("a", encoding="utf-8", buffering=1)
            self.record({"type": "session_start"})

    def record(self, message: dict) -> None:
        if self.handle is not None:
            # ponytail: line-buffered local writes; move to a queue if feed volume stalls reads.
            self.handle.write(
                json.dumps(
                    {
                        "received_ns": time.time_ns(),
                        "session": self.session,
                        "data": message,
                    },
                    separators=(",", ":"),
                )
                + "\n"
            )

    def close(self) -> None:
        if self.handle is not None:
            try:
                self.record({"type": "session_end"})
            finally:
                self.handle.close()


class BookReplay:
    """Sequence-checked books shared by quote export and execution research."""

    def __init__(self):
        self.session = None
        self.books: dict[str, OrderBook] = {}
        self.updated_ns: dict[str, int] = {}

    def ingest(self, row: dict) -> set[str]:
        changed: set[str] = set()
        received_ns = row["received_ns"]
        if row["session"] != self.session:
            changed.update(self.books)
            self.books.clear()
            self.updated_ns.clear()
            self.session = row["session"]
        data = row["data"]
        kind, msg = data.get("type"), data.get("msg") or {}
        if kind == "session_end":
            changed.update(self.books)
            self.books.clear()
            self.updated_ns.clear()
            return changed
        if kind not in {"orderbook_snapshot", "orderbook_delta"}:
            return changed
        ticker, seq = msg.get("market_ticker"), data.get("seq")
        if not ticker:
            return changed
        if not isinstance(seq, int):
            if kind == "orderbook_delta" and ticker in self.books:
                self.books.pop(ticker)
                self.updated_ns.pop(ticker, None)
                changed.add(ticker)
            return changed
        if kind == "orderbook_snapshot":
            book = self.books[ticker] = OrderBook(ticker)
            book.load_ws_snapshot(msg, seq)
        else:
            book = self.books.get(ticker)
            if book is None or not book.initialized or book.needs_resync:
                return changed
            if not book.apply_delta_with_seq(seq, msg):
                if not book.needs_resync:
                    return changed
        if book.needs_resync:
            self.books.pop(ticker, None)
            self.updated_ns.pop(ticker, None)
        else:
            self.updated_ns[ticker] = received_ns
        changed.add(ticker)
        return changed


def load_quote_tapes(path: Path) -> dict[str, list[dict]]:
    """Replay only continuous snapshot/delta segments; use local receipt time."""
    tapes: dict[str, list[dict]] = {}
    replay = BookReplay()
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            for ticker in replay.ingest(row):
                book = replay.books.get(ticker)
                bid, ask, _, _ = book.get_best_prices() if book else (None,) * 4
                tapes.setdefault(ticker, []).append(
                    {"ts": row["received_ns"] / 1e9, "yes_bid_cents": bid, "yes_ask_cents": ask}
                )
    return tapes
