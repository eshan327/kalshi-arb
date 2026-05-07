from __future__ import annotations

import time
from collections import Counter
from collections import deque
from threading import RLock
from typing import Any

from core.config import (
    EXECUTION_EVENTS_MAXLEN,
    EXECUTION_EVENTS_PATH,
    EXECUTION_PERFORMANCE_PATH,
)
from engine.execution.persistence import append_jsonl

_metrics_lock = RLock()

_execution_events: deque[dict[str, Any]] = deque(maxlen=EXECUTION_EVENTS_MAXLEN)
_fill_events: deque[dict[str, Any]] = deque(maxlen=EXECUTION_EVENTS_MAXLEN)
_confidence_samples: deque[dict[str, Any]] = deque(maxlen=EXECUTION_EVENTS_MAXLEN)
_timing_samples: deque[dict[str, Any]] = deque(maxlen=EXECUTION_EVENTS_MAXLEN)
_rejection_samples: deque[dict[str, Any]] = deque(maxlen=EXECUTION_EVENTS_MAXLEN)
_pnl_curve: deque[dict[str, Any]] = deque(maxlen=EXECUTION_EVENTS_MAXLEN)
_edge_curve: deque[dict[str, Any]] = deque(maxlen=EXECUTION_EVENTS_MAXLEN)
_win_rate_curve: deque[dict[str, Any]] = deque(maxlen=EXECUTION_EVENTS_MAXLEN)
_window_summaries: deque[dict[str, Any]] = deque(maxlen=400)

_realized_pnl_cents: int = 0
_edge_captured_cents: float = 0.0
_settled_wins: int = 0
_settled_losses: int = 0
_settled_trades: int = 0
_fill_count: int = 0
_maker_fills_total: int = 0
_taker_fills_total: int = 0
_fallback_fills_total: int = 0
_daily_pnl_day: int = int(time.time() // 86400)
_daily_realized_pnl_cents: int = 0
_paper_account_snapshot: dict[str, Any] = {}
_window_participation_latest: dict[str, dict[str, Any]] = {}

_LATENCY_BOUNDS_MS = (50.0, 100.0, 200.0, 400.0, 800.0, 1600.0)
_SPREAD_BOUNDS_CENTS = (1.0, 2.0, 3.0, 5.0, 8.0, 13.0)


def _utc_day(ts: float) -> int:
    return int(float(ts) // 86400)


def _roll_daily_bucket_if_needed(ts: float) -> None:
    global _daily_pnl_day, _daily_realized_pnl_cents
    day = _utc_day(ts)
    if day == _daily_pnl_day:
        return
    _daily_pnl_day = day
    _daily_realized_pnl_cents = 0


def _event(kind: str, payload: dict[str, Any] | None = None, *, ts: float | None = None) -> dict[str, Any]:
    out = {
        "ts": float(time.time() if ts is None else ts),
        "kind": str(kind),
    }
    if isinstance(payload, dict):
        out.update(payload)
    return out


def record_execution_event(kind: str, payload: dict[str, Any] | None = None, *, persist: bool = True) -> dict[str, Any]:
    event = _event(kind, payload)
    with _metrics_lock:
        _execution_events.append(event)
    if persist:
        append_jsonl(EXECUTION_EVENTS_PATH, event)
    return event


def record_policy_decision(
    *,
    reason: str,
    signal_payload: dict[str, Any] | None,
) -> None:
    has_signal = bool(signal_payload)
    payload: dict[str, Any] = {
        "reason": reason,
        "has_signal": has_signal,
        "rejection_reason": None if has_signal else str(reason or "unknown"),
    }
    if isinstance(signal_payload, dict):
        payload.update(signal_payload)

    event = record_execution_event("decision", payload)

    if not isinstance(signal_payload, dict):
        with _metrics_lock:
            _rejection_samples.append(
                {
                    "ts": float(event["ts"]),
                    "reason": str(reason or "unknown"),
                }
            )
        return

    confidence = signal_payload.get("confidence")
    seconds_to_expiry = signal_payload.get("seconds_to_expiry")
    with _metrics_lock:
        if isinstance(confidence, (int, float)):
            _confidence_samples.append({"ts": event["ts"], "confidence": float(confidence)})
        if isinstance(seconds_to_expiry, (int, float)):
            _timing_samples.append(
                {
                    "ts": event["ts"],
                    "seconds_to_expiry": float(seconds_to_expiry),
                    "confidence": float(confidence) if isinstance(confidence, (int, float)) else None,
                    "side": signal_payload.get("side"),
                    "count": int(signal_payload.get("count", 0)),
                }
            )


def record_fill(fill_payload: dict[str, Any]) -> None:
    global _fill_count, _edge_captured_cents, _maker_fills_total, _taker_fills_total, _fallback_fills_total

    fill = record_execution_event("fill", fill_payload)
    with _metrics_lock:
        _fill_events.append(fill)
        _fill_count += 1

        if bool(fill_payload.get("is_taker")):
            _taker_fills_total += 1
        else:
            _maker_fills_total += 1

        if bool(fill_payload.get("is_fallback_attempt")):
            _fallback_fills_total += 1

        edge_cents = fill_payload.get("expected_edge_cents")
        if isinstance(edge_cents, (int, float)):
            _edge_captured_cents += float(edge_cents)
            _edge_curve.append(
                {
                    "ts": fill["ts"],
                    "value": round(_edge_captured_cents, 6),
                }
            )

    append_jsonl(EXECUTION_PERFORMANCE_PATH, fill)


def record_realized_pnl_delta(*, delta_cents: int, source: str, market_ticker: str | None) -> None:
    global _realized_pnl_cents, _daily_realized_pnl_cents, _settled_trades, _settled_wins, _settled_losses

    if int(delta_cents) == 0:
        return

    ts = time.time()
    delta = int(delta_cents)

    with _metrics_lock:
        _roll_daily_bucket_if_needed(ts)
        _realized_pnl_cents += delta
        _daily_realized_pnl_cents += delta
        _settled_trades += 1
        if delta > 0:
            _settled_wins += 1
        else:
            _settled_losses += 1

        _pnl_curve.append({"ts": ts, "value": _realized_pnl_cents})
        win_rate = (_settled_wins / _settled_trades) if _settled_trades > 0 else 0.0
        _win_rate_curve.append({"ts": ts, "value": round(win_rate, 6)})

    event = record_execution_event(
        "realized_pnl_delta",
        {
            "delta_cents": delta,
            "source": source,
            "market_ticker": market_ticker,
            "pnl_cents": _realized_pnl_cents,
            "daily_pnl_cents": _daily_realized_pnl_cents,
        },
    )
    append_jsonl(EXECUTION_PERFORMANCE_PATH, event)


def get_daily_realized_pnl_cents() -> int:
    with _metrics_lock:
        _roll_daily_bucket_if_needed(time.time())
        return int(_daily_realized_pnl_cents)


def record_paper_account_snapshot(snapshot: dict[str, Any]) -> None:
    if not isinstance(snapshot, dict):
        return
    with _metrics_lock:
        _paper_account_snapshot.clear()
        _paper_account_snapshot.update(dict(snapshot))


def record_window_participation(snapshot: dict[str, Any]) -> None:
    if not isinstance(snapshot, dict):
        return
    window_id = snapshot.get("window_id")
    if not isinstance(window_id, str) or not window_id:
        return

    event = record_execution_event("window_participation", snapshot)
    with _metrics_lock:
        _window_participation_latest[window_id] = dict(event)
        if len(_window_participation_latest) > 300:
            oldest = sorted(
                _window_participation_latest.items(),
                key=lambda item: float(item[1].get("ts", 0.0)),
            )[:50]
            for key, _ in oldest:
                _window_participation_latest.pop(key, None)

    append_jsonl(EXECUTION_PERFORMANCE_PATH, event)


def record_window_summary(summary: dict[str, Any]) -> None:
    if not isinstance(summary, dict):
        return
    event = record_execution_event("window_summary", summary)
    with _metrics_lock:
        _window_summaries.append(dict(event))
    append_jsonl(EXECUTION_PERFORMANCE_PATH, event)


def _confidence_histogram(samples: list[dict[str, Any]]) -> dict[str, int]:
    bins = {f"{start}-{start + 5}": 0 for start in range(0, 100, 5)}

    for item in samples:
        conf = item.get("confidence")
        if not isinstance(conf, (int, float)):
            continue
        pct = max(0.0, min(100.0, float(conf) * 100.0))
        idx = min(95, int(pct // 5) * 5)
        key = f"{idx}-{idx + 5}"
        bins[key] += 1

    return bins


def _numeric_bins(values: list[float], bounds: tuple[float, ...]) -> dict[str, int]:
    clean = [float(v) for v in values if isinstance(v, (int, float))]
    if not clean:
        return {}

    labels: list[str] = []
    lower = 0.0
    for bound in bounds:
        labels.append(f"{lower:g}-{bound:g}")
        lower = float(bound)
    labels.append(f">={bounds[-1]:g}")

    out = {label: 0 for label in labels}
    for value in clean:
        assigned = False
        lower_edge = 0.0
        for bound in bounds:
            if lower_edge <= value < bound:
                out[f"{lower_edge:g}-{bound:g}"] += 1
                assigned = True
                break
            lower_edge = float(bound)
        if not assigned:
            out[f">={bounds[-1]:g}"] += 1

    return out


def _reason_hist(samples: list[dict[str, Any]]) -> dict[str, int]:
    reasons = [str(item.get("reason") or "unknown") for item in samples]
    if not reasons:
        return {}
    counts = Counter(reasons)
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def _series_since(series: deque[dict[str, Any]], cutoff_ts: float, limit: int = 1200) -> list[dict[str, Any]]:
    out = [item for item in series if float(item.get("ts", 0.0)) >= cutoff_ts]
    if len(out) > limit:
        return out[-limit:]
    return out


def get_runtime_stats_snapshot(window_seconds: int = 900) -> dict[str, Any]:
    now = time.time()
    cutoff = now - max(1, int(window_seconds))

    with _metrics_lock:
        recent_fills = [ev for ev in _fill_events if float(ev.get("ts", 0.0)) >= cutoff]
        recent_confidence = [ev for ev in _confidence_samples if float(ev.get("ts", 0.0)) >= cutoff]
        recent_timing = [ev for ev in _timing_samples if float(ev.get("ts", 0.0)) >= cutoff]
        recent_rejections = [ev for ev in _rejection_samples if float(ev.get("ts", 0.0)) >= cutoff]

        settled_total = max(0, int(_settled_trades))
        win_rate = (float(_settled_wins) / settled_total) if settled_total > 0 else None

        pnl_curve = _series_since(_pnl_curve, cutoff)
        edge_curve = _series_since(_edge_curve, cutoff)
        win_rate_curve = _series_since(_win_rate_curve, cutoff)

        maker_fills_window = sum(1 for ev in recent_fills if not bool(ev.get("is_taker")))
        taker_fills_window = sum(1 for ev in recent_fills if bool(ev.get("is_taker")))
        fallback_fills_window = sum(1 for ev in recent_fills if bool(ev.get("is_fallback_attempt")))

        latency_values: list[float] = []
        spread_values: list[float] = []
        for ev in recent_fills:
            latency_raw = ev.get("fill_latency_ms")
            if isinstance(latency_raw, (int, float)):
                latency_values.append(float(latency_raw))

            spread_raw = ev.get("spread_cents")
            if isinstance(spread_raw, (int, float)):
                spread_values.append(float(spread_raw))
        latency_bins = _numeric_bins(latency_values, _LATENCY_BOUNDS_MS)
        spread_bins = _numeric_bins(spread_values, _SPREAD_BOUNDS_CENTS)

        windows_recent = sorted(
            _window_participation_latest.values(),
            key=lambda item: float(item.get("ts", 0.0)),
            reverse=True,
        )[:30]
        windows_total = len(windows_recent)
        windows_with_fill = sum(1 for item in windows_recent if bool(item.get("has_fill")))

        summaries_recent = [
            item for item in _window_summaries if float(item.get("ts", 0.0)) >= cutoff
        ]
        if not summaries_recent:
            summaries_recent = list(_window_summaries)[-20:]

        settled_windows_total = len(summaries_recent)
        settled_windows_with_fill = sum(1 for item in summaries_recent if bool(item.get("has_fill")))
        window_fill_rate = (
            float(settled_windows_with_fill) / float(settled_windows_total)
            if settled_windows_total > 0
            else None
        )

        participation_curve: list[dict[str, Any]] = []
        running_total = 0
        running_filled = 0
        for row in sorted(summaries_recent, key=lambda item: float(item.get("ts", 0.0))):
            running_total += 1
            if bool(row.get("has_fill")):
                running_filled += 1
            participation_curve.append(
                {
                    "ts": float(row.get("ts", 0.0)),
                    "value": (float(running_filled) / float(running_total)) if running_total > 0 else 0.0,
                }
            )

        return {
            "window_seconds": int(window_seconds),
            "pnl_cents": int(_realized_pnl_cents),
            "pnl_dollars": round(float(_realized_pnl_cents) / 100.0, 4),
            "daily_pnl_cents": int(_daily_realized_pnl_cents),
            "daily_pnl_dollars": round(float(_daily_realized_pnl_cents) / 100.0, 4),
            "win_rate": win_rate,
            "wins": int(_settled_wins),
            "losses": int(_settled_losses),
            "settled_trades": settled_total,
            "fills_total": int(_fill_count),
            "fills_recent_window": len(recent_fills),
            "edge_captured_cents": round(float(_edge_captured_cents), 6),
            "edge_captured_dollars": round(float(_edge_captured_cents) / 100.0, 6),
            "maker_fills_total": int(_maker_fills_total),
            "taker_fills_total": int(_taker_fills_total),
            "fallback_fills_total": int(_fallback_fills_total),
            "maker_fills_window": int(maker_fills_window),
            "taker_fills_window": int(taker_fills_window),
            "fallback_fills_window": int(fallback_fills_window),
            "rejection_reasons": _reason_hist(recent_rejections),
            "rejection_reasons_total": _reason_hist(list(_rejection_samples)),
            "fill_latency_bins_ms": latency_bins,
            "fill_latency_bins": latency_bins,
            "spread_bins_cents": spread_bins,
            "fill_spread_bins": spread_bins,
            "confidence_bins": _confidence_histogram(recent_confidence),
            "order_timing_events": list(recent_timing)[-400:],
            "window_participation_recent": windows_recent,
            "windows_total_recent": int(windows_total),
            "windows_with_fill_recent": int(windows_with_fill),
            "window_count_settled": int(settled_windows_total),
            "window_count_with_fill": int(settled_windows_with_fill),
            "window_fill_rate_recent": window_fill_rate,
            "window_summaries": list(summaries_recent)[-60:],
            "window_fill_rate_curve": participation_curve,
            "pnl_curve": pnl_curve,
            "edge_curve": edge_curve,
            "win_rate_curve": win_rate_curve,
            "paper": dict(_paper_account_snapshot),
        }


def get_execution_events(limit: int = 200) -> list[dict[str, Any]]:
    with _metrics_lock:
        if limit <= 0:
            return []
        return list(_execution_events)[-limit:]


def get_fill_events(limit: int = 200) -> list[dict[str, Any]]:
    with _metrics_lock:
        if limit <= 0:
            return []
        return list(_fill_events)[-limit:]
