from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from pathlib import Path
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


def analyze(events: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    decisions = [
        _decision_row(event)
        for event in events
        if event.get("kind") == "strategy_decision"
    ]
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
        "horizon_coverage": horizon_coverage,
        "sequence_gaps": gaps,
        "note": (
            "This summarizes observed forward data. A signal is not proof of a fill; "
            "use captured orderbook updates plus actual order/fill events to assess "
            "sub-minute execution."
        ),
    }
    return decisions, summary


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Summarize forward late-market Kalshi research capture."
    )
    parser.add_argument("capture_path")
    parser.add_argument("--output-dir", default="output/live_capture")
    args = parser.parse_args()

    events = load_events(Path(args.capture_path))
    decisions, summary = analyze(events)
    out = Path(args.output_dir)
    _write_csv(out / "decisions.csv", decisions)
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
