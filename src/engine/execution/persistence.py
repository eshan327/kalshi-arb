from __future__ import annotations

import json
import os
from collections import deque
from threading import Lock
from typing import Any

_path_locks_guard = Lock()
_path_locks: dict[str, Lock] = {}


def _get_path_lock(path: str) -> Lock:
    with _path_locks_guard:
        lock = _path_locks.get(path)
        if lock is None:
            lock = Lock()
            _path_locks[path] = lock
        return lock


def append_jsonl(path: str, record: dict[str, Any]) -> None:
    """Appends one JSON object per line with a per-path mutex for thread safety."""
    abs_path = os.path.abspath(path)
    parent = os.path.dirname(abs_path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    line = json.dumps(record, separators=(",", ":"), ensure_ascii=True)
    lock = _get_path_lock(abs_path)
    with lock:
        with open(abs_path, "a", encoding="utf-8") as handle:
            handle.write(line)
            handle.write("\n")


def load_recent_jsonl(path: str, limit: int) -> list[dict[str, Any]]:
    """Loads up to the newest limit JSONL rows, skipping malformed lines."""
    cap = max(0, int(limit))
    if cap <= 0:
        return []

    abs_path = os.path.abspath(path)
    if not os.path.exists(abs_path):
        return []

    rows: deque[dict[str, Any]] = deque(maxlen=cap)
    lock = _get_path_lock(abs_path)
    with lock:
        with open(abs_path, "r", encoding="utf-8") as handle:
            for line in handle:
                raw = line.strip()
                if not raw:
                    continue
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict):
                    rows.append(parsed)

    return list(rows)


def clear_jsonl(path: str) -> None:
    abs_path = os.path.abspath(path)
    lock = _get_path_lock(abs_path)
    with lock:
        if os.path.exists(abs_path):
            os.remove(abs_path)


def count_jsonl_rows(path: str) -> int:
    abs_path = os.path.abspath(path)
    if not os.path.exists(abs_path):
        return 0

    lock = _get_path_lock(abs_path)
    with lock:
        with open(abs_path, "r", encoding="utf-8") as handle:
            return sum(1 for _ in handle)
