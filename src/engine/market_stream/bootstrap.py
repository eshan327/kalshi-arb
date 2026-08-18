from __future__ import annotations

from engine.book_microstructure import on_live_orderbook_update
from engine.orderbook import OrderBook

BufferedDelta = tuple[int, dict]
def replay_buffered_deltas(
    book: OrderBook, buffered_deltas: list[BufferedDelta]
) -> int:
    applied = 0
    for buffered_seq, buffered_msg in sorted(buffered_deltas, key=lambda item: item[0]):
        if book.apply_delta_with_seq(buffered_seq, buffered_msg):
            applied += 1
            on_live_orderbook_update(book)
        elif buffered_seq < (book.expected_seq or 0):
            continue
        else:
            break
    return applied
