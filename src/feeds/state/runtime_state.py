from __future__ import annotations

from feeds.state.book_store import reset_exchange_books
from feeds.state.tick_store import reset_tick_state


def reset_brti_runtime_state(asset: str) -> None:
    """Clears all feed runtime state before starting an asset's streams."""
    reset_exchange_books()
    reset_tick_state(asset)
