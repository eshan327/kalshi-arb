from __future__ import annotations

from feeds.state.book_store import reset_exchange_books
from feeds.state.diagnostics_store import reset_diagnostics_state
from feeds.state.tick_store import reset_tick_state


def reset_brti_runtime_state(asset: str) -> None:
    """Clears all feed runtime state so BTC/ETH switches do not leak historical state."""
    reset_exchange_books()
    reset_tick_state(asset)
    reset_diagnostics_state()
