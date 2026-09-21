"""One coalescing wakeup shared by streams and the strategy."""

import asyncio

changed = asyncio.Event()
_loop: asyncio.AbstractEventLoop | None = None


def bind() -> None:
    global _loop
    _loop = asyncio.get_running_loop()


def notify() -> None:
    if _loop is not None and not _loop.is_closed():
        _loop.call_soon_threadsafe(changed.set)
    else:
        changed.set()
