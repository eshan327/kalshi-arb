"""Opt-in raw public feed capture and sequence-checked quote replay."""

from __future__ import annotations

import json
import time
from pathlib import Path

from core.config import KALSHI_ENV
from data.orderbook import OrderBook


class FeedCapture:
    def __init__(self, path: str | None, session: str, *, endpoint: str):
        self.session = session
        self.handle = None
        if path:
            target = Path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            self.handle = target.open("a", encoding="utf-8", buffering=1)
            try:
                self.record({"type": "session_start", "book_price_mode": "native_bids",
                             "environment": KALSHI_ENV, "endpoint": endpoint})
            except Exception:
                self.handle.close()
                raise

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
        if kind == "session_start" and data.get("book_price_mode") != "native_bids":
            raise ValueError("Prediction capture uses an older book price mode; record a new capture")
        if kind == "session_end":
            changed.update(self.books)
            self.books.clear()
            self.updated_ns.clear()
            return changed
        if kind not in {"orderbook_snapshot", "orderbook_delta"}:
            return changed
        ticker, sid, seq = msg.get("market_ticker"), data.get("sid"), data.get("seq")
        if not ticker:
            return changed
        if type(sid) is not int or type(seq) is not int:
            if ticker in self.books:
                self.books[ticker].needs_resync = True
                self.updated_ns.pop(ticker, None)
                changed.add(ticker)
            return changed
        if kind == "orderbook_snapshot":
            book = self.books.setdefault(ticker, OrderBook(ticker))
            if not book.load_ws_snapshot(msg, seq, sid):
                return changed
        else:
            book = self.books.get(ticker)
            if book is None or not book.initialized or book.needs_resync:
                return changed
            if not book.apply_delta_with_seq(seq, msg, sid):
                if not book.needs_resync:
                    return changed
        if book.needs_resync:
            self.updated_ns.pop(ticker, None)
        else:
            self.updated_ns[ticker] = received_ns
        changed.add(ticker)
        return changed


def iter_capture(path: Path, *, environment: str, endpoint: str):
    """Validate session provenance and bound replay to the recorded receipt interval."""
    if environment not in {"demo", "prod"} or not isinstance(endpoint, str) or not endpoint:
        raise ValueError("Capture needs an expected environment and endpoint")
    previous = -1
    session = None
    session_open = False
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            received_ns = row["received_ns"]
            if type(received_ns) is not int or received_ns < previous:
                raise ValueError("Capture receipt times must be monotonic nanoseconds")
            data = row["data"]
            if not isinstance(row.get("session"), str) or not row["session"]:
                raise ValueError("Capture needs a nonempty session ID")
            if row["session"] != session:
                if data.get("type") != "session_start":
                    raise ValueError("Capture needs session provenance; record a new capture")
                if session is not None and session_open:
                    yield {"received_ns": min(previous + 1, received_ns), "session": session,
                           "data": {"type": "session_end"}}
                session = row["session"]
            if data.get("type") == "session_start" and (
                data.get("environment") != environment or data.get("endpoint") != endpoint
            ):
                raise ValueError("Capture environment/endpoint mismatch or missing provenance; record a new capture")
            previous = received_ns
            session_open = data.get("type") != "session_end"
            yield row
    if session_open:
        # A missing close marker cannot extend coverage past the last recorded receipt.
        yield {"received_ns": previous + 1, "session": session, "data": {"type": "session_end"}}


def load_quote_tapes(path: Path, *, environment: str, endpoint: str) -> dict[str, list[dict]]:
    """Replay only continuous snapshot/delta segments; use local receipt time."""
    tapes: dict[str, list[dict]] = {}
    replay = BookReplay()
    for row in iter_capture(path, environment=environment, endpoint=endpoint):
        for ticker in replay.ingest(row):
            book = replay.books.get(ticker)
            bid, ask, _, _ = book.get_best_prices() if book else (None,) * 4
            tapes.setdefault(ticker, []).append(
                {"ts": row["received_ns"] / 1e9, "yes_bid_cents": bid, "yes_ask_cents": ask}
            )
    return tapes
