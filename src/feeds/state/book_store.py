from __future__ import annotations

import time
from threading import RLock
from typing import Any, TypedDict


class ExchangeBook(TypedDict):
    bids: dict[float, float]
    asks: dict[float, float]
    last_update: float


_exchange_books: dict[str, ExchangeBook] = {}
_book_lock = RLock()


def safe_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def init_exchange_book(exchange: str) -> None:
    with _book_lock:
        _exchange_books[exchange] = {
            "bids": {},
            "asks": {},
            "last_update": 0,
        }


def update_level(exchange: str, side: str, price: float, size: float) -> None:
    with _book_lock:
        if exchange not in _exchange_books:
            init_exchange_book(exchange)

        side_book = _exchange_books[exchange][side]
        if size <= 0:
            side_book.pop(price, None)
        else:
            side_book[price] = size

        _exchange_books[exchange]["last_update"] = time.time()


def replace_full_book(exchange: str, bids: dict[float, float], asks: dict[float, float]) -> None:
    with _book_lock:
        if exchange not in _exchange_books:
            init_exchange_book(exchange)

        _exchange_books[exchange]["bids"] = bids
        _exchange_books[exchange]["asks"] = asks
        _exchange_books[exchange]["last_update"] = time.time()


def get_exchange_books_ref() -> dict[str, ExchangeBook]:
    return _exchange_books


def reset_exchange_books() -> None:
    with _book_lock:
        _exchange_books.clear()
