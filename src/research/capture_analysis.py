from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from pathlib import Path
from statistics import median
from typing import Any


HORIZONS = (1, 5, 10, 20, 30, 45, 60)


def load_events(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, raw in enumerate(handle, 1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                row = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON on line {line_number}") from exc
            if isinstance(row, dict):
                events.append(row)
    events.sort(key=lambda row: float(row.get("receipt_ts") or 0.0))
    return events


def _decision_row(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    pricing = payload.get("pricing") if isinstance(payload.get("pricing"), dict) else {}
    diagnostics = (
        payload.get("diagnostics") if isinstance(payload.get("diagnostics"), dict) else {}
    )
    signal = payload.get("signal") if isinstance(payload.get("signal"), dict) else {}
    return {
        "receipt_ts": event.get("receipt_ts"),
        "market_ticker": event.get("market_ticker"),
        "seconds_to_expiry": event.get("seconds_to_expiry"),
        "reason": payload.get("reason"),
        "execution_mode": payload.get("execution_mode"),
        "armed": payload.get("armed"),
        "p_model": pricing.get("p_model"),
        "sigma_annual": pricing.get("sigma_annual"),
        "sigma_applied": pricing.get("sigma_override_applied"),
        "vol_window_seconds": pricing.get("vol_window_seconds"),
        "vol_window_policy": pricing.get("vol_window_policy"),
        "twap_samples_observed": pricing.get("twap_samples_observed"),
        "yes_bid_cents": diagnostics.get("yes_bid_cents"),
        "yes_ask_cents": diagnostics.get("yes_ask_cents"),
        "no_bid_cents": diagnostics.get("no_bid_cents"),
        "no_ask_cents": diagnostics.get("no_ask_cents"),
        "best_edge_cents": max(
            [
                value
                for value in (
                    diagnostics.get("edge_yes_cents"),
                    diagnostics.get("edge_no_cents"),
                )
                if isinstance(value, (int, float))
            ],
            default=None,
        ),
        "required_edge_cents": diagnostics.get("required_taker_edge_cents"),
        "orderbook_age_seconds": diagnostics.get("orderbook_age_seconds"),
        "signal_action": signal.get("action"),
        "signal_side": signal.get("side"),
        "signal_count": signal.get("count"),
        "signal_limit_cents": signal.get("quote_price_cents"),
        "signal_edge_cents": signal.get("edge_cents"),
    }


def _trade_row(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    return {
        "receipt_ts": event.get("receipt_ts"),
        "market_ticker": event.get("market_ticker"),
        "seconds_to_expiry": event.get("seconds_to_expiry"),
        "trade_id": payload.get("trade_id"),
        "yes_price_dollars": payload.get("yes_price_dollars"),
        "no_price_dollars": payload.get("no_price_dollars"),
        "count_fp": payload.get("count_fp"),
        "taker_outcome_side": payload.get("taker_outcome_side")
        or payload.get("taker_side"),
        "taker_book_side": payload.get("taker_book_side"),
        "is_block_trade": bool(payload.get("is_block_trade", False)),
        "source_ts": payload.get("ts"),
        "source_ts_ms": payload.get("ts_ms"),
    }


def _order_rows(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    submissions: dict[str, dict[str, Any]] = {}
    completions: dict[str, dict[str, Any]] = {}
    first_fills: dict[str, dict[str, Any]] = {}

    for event in events:
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        client_id = payload.get("client_order_id")
        if not isinstance(client_id, str) or not client_id:
            continue
        kind = event.get("kind")
        if kind == "order_submission":
            submissions.setdefault(client_id, event)
        elif kind in {"order_result", "order_error"}:
            completions.setdefault(client_id, event)
        elif kind == "fill":
            first_fills.setdefault(client_id, event)

    rows: list[dict[str, Any]] = []
    for client_id, submitted in sorted(
        submissions.items(), key=lambda item: float(item[1].get("receipt_ts") or 0.0)
    ):
        payload = (
            submitted.get("payload")
            if isinstance(submitted.get("payload"), dict)
            else {}
        )
        completion = completions.get(client_id)
        completion_payload = (
            completion.get("payload")
            if completion and isinstance(completion.get("payload"), dict)
            else {}
        )
        result = (
            completion_payload.get("result")
            if isinstance(completion_payload.get("result"), dict)
            else {}
        )
        order = result.get("order") if isinstance(result.get("order"), dict) else {}
        fill = first_fills.get(client_id)
        submitted_ts = float(submitted.get("receipt_ts") or 0.0)
        completion_ts = (
            float(completion.get("receipt_ts") or 0.0) if completion else None
        )
        fill_ts = float(fill.get("receipt_ts") or 0.0) if fill else None
        rows.append(
            {
                "client_order_id": client_id,
                "market_ticker": submitted.get("market_ticker"),
                "seconds_to_expiry": submitted.get("seconds_to_expiry"),
                "execution_mode": payload.get("execution_mode"),
                "side": payload.get("side"),
                "action": payload.get("action"),
                "requested_count": payload.get("count"),
                "limit_price_cents": payload.get("price_cents"),
                "submission_receipt_ts": submitted_ts,
                "completion_kind": None if completion is None else completion.get("kind"),
                "completion_receipt_ts": completion_ts,
                "response_latency_ms": (
                    None
                    if completion_ts is None
                    else round((completion_ts - submitted_ts) * 1000.0, 3)
                ),
                "first_fill_receipt_ts": fill_ts,
                "first_fill_latency_ms": (
                    None if fill_ts is None else round((fill_ts - submitted_ts) * 1000.0, 3)
                ),
                "fill_count": order.get("fill_count"),
                "order_id": order.get("order_id"),
                "error": completion_payload.get("error"),
                "http_status": completion_payload.get("http_status"),
            }
        )
    return rows


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields = list(rows[0])
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def analyze(
    events: list[dict[str, Any]],
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, Any],
]:
    decisions = [
        _decision_row(event)
        for event in events
        if event.get("kind") == "strategy_decision"
    ]
    trades = [_trade_row(event) for event in events if event.get("kind") == "trade"]
    orders = _order_rows(events)
    event_counts = Counter(str(event.get("kind") or "unknown") for event in events)
    reasons = Counter(str(row.get("reason") or "unknown") for row in decisions)

    horizon_coverage: list[dict[str, Any]] = []
    for horizon in HORIZONS:
        within = [
            event
            for event in events
            if isinstance(event.get("seconds_to_expiry"), (int, float))
            and 0 <= float(event["seconds_to_expiry"]) <= horizon
        ]
        decision_rows = [
            row
            for row in decisions
            if isinstance(row.get("seconds_to_expiry"), (int, float))
            and 0 <= float(row["seconds_to_expiry"]) <= horizon
        ]
        trade_rows = [
            row
            for row in trades
            if isinstance(row.get("seconds_to_expiry"), (int, float))
            and 0 <= float(row["seconds_to_expiry"]) <= horizon
        ]
        horizon_coverage.append(
            {
                "horizon_seconds": horizon,
                "events": len(within),
                "markets": len(
                    {str(event.get("market_ticker") or "") for event in within}
                    - {""}
                ),
                "decisions": len(decision_rows),
                "buy_signals": sum(
                    row.get("signal_action") == "buy" for row in decision_rows
                ),
                "public_trades": len(trade_rows),
                "non_block_public_trades": sum(
                    not bool(row.get("is_block_trade")) for row in trade_rows
                ),
                "public_trade_contracts": sum(
                    float(row["count_fp"])
                    for row in trade_rows
                    if not bool(row.get("is_block_trade"))
                    and row.get("count_fp") is not None
                ),
            }
        )

    sequences: dict[tuple[str, int], list[int]] = defaultdict(list)
    for event in events:
        sid = event.get("sid")
        seq = event.get("seq")
        if isinstance(sid, int) and isinstance(seq, int):
            sequences[(str(event.get("kind") or ""), sid)].append(seq)
    gaps = []
    for (kind, sid), values in sequences.items():
        ordered = sorted(set(values))
        missing = sum(max(0, b - a - 1) for a, b in zip(ordered, ordered[1:]))
        if missing:
            gaps.append(
                {
                    "kind": kind,
                    "sid": sid,
                    "first_seq": ordered[0],
                    "last_seq": ordered[-1],
                    "missing_sequence_numbers": missing,
                }
            )

    buy_signals = [row for row in decisions if row.get("signal_action") == "buy"]
    response_latencies = [
        float(row["response_latency_ms"])
        for row in orders
        if row.get("response_latency_ms") is not None
    ]
    fill_latencies = [
        float(row["first_fill_latency_ms"])
        for row in orders
        if row.get("first_fill_latency_ms") is not None
    ]
    summary = {
        "events": len(events),
        "markets": len(
            {str(event.get("market_ticker") or "") for event in events} - {""}
        ),
        "event_counts": dict(sorted(event_counts.items())),
        "decisions": len(decisions),
        "buy_signals": len(buy_signals),
        "sell_signals": sum(row.get("signal_action") == "sell" for row in decisions),
        "decision_reasons": dict(sorted(reasons.items())),
        "public_trades": len(trades),
        "non_block_public_trades": sum(
            not bool(row.get("is_block_trade")) for row in trades
        ),
        "orders_submitted": len(orders),
        "orders_with_fill": sum(row.get("first_fill_receipt_ts") is not None for row in orders),
        "median_order_response_latency_ms": (
            None if not response_latencies else round(median(response_latencies), 3)
        ),
        "median_first_fill_latency_ms": (
            None if not fill_latencies else round(median(fill_latencies), 3)
        ),
        "horizon_coverage": horizon_coverage,
        "sequence_gaps": gaps,
        "note": (
            "This summarizes observed forward data. A signal is not proof of a fill; "
            "use captured orderbook updates plus actual order/fill events to assess "
            "sub-minute execution."
        ),
    }
    return decisions, trades, orders, summary


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Summarize forward late-market Kalshi research capture."
    )
    parser.add_argument("capture_path")
    parser.add_argument("--output-dir", default="output/live_capture")
    args = parser.parse_args()

    events = load_events(Path(args.capture_path))
    decisions, trades, orders, summary = analyze(events)
    out = Path(args.output_dir)
    _write_csv(out / "decisions.csv", decisions)
    _write_csv(out / "public_trades.csv", trades)
    _write_csv(out / "orders.csv", orders)
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
