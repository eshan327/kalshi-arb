"""Replay candidate taker entries against captured Kalshi depth and settlement."""

from __future__ import annotations

import argparse
import csv
import json
import math
from collections import Counter
from decimal import Decimal, ROUND_CEILING, ROUND_DOWN
from pathlib import Path

from core.markets import parse_iso8601_to_epoch
from data.capture import BookReplay

MICRODOLLAR = Decimal("0.000001")
CONTRACT_STEP = Decimal("0.01")
TAKER_COEFFICIENT = Decimal("0.07")


def _decimal(value: str, name: str, *, minimum: Decimal = Decimal("0")) -> Decimal:
    try:
        number = Decimal(value)
    except Exception as exc:
        raise ValueError(f"Invalid {name}: {value!r}") from exc
    if not number.is_finite() or number < minimum:
        raise ValueError(f"Invalid {name}: {value!r}")
    return number


def load_signals(path: Path, markets: dict[str, dict]) -> list[dict]:
    signals, seen = [], set()
    with path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        required = {
            "market_ticker", "decision_ts", "side", "contracts", "limit_price_cents"
        }
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            raise ValueError(f"Signals CSV needs columns: {', '.join(sorted(required))}")
        for number, row in enumerate(reader, 2):
            ticker = row["market_ticker"]
            if ticker not in markets or ticker in seen:
                raise ValueError(f"Line {number}: unknown or repeated market {ticker!r}")
            seen.add(ticker)
            side = (row["side"] or "").lower()
            if side not in {"yes", "no"}:
                raise ValueError(f"Line {number}: side must be yes or no")
            try:
                decision_ts = float(row["decision_ts"])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Line {number}: invalid decision_ts") from exc
            if not math.isfinite(decision_ts) or decision_ts <= 0:
                raise ValueError(f"Line {number}: invalid decision_ts")
            count = _decimal(row["contracts"], "contracts", minimum=CONTRACT_STEP)
            limit = _decimal(row["limit_price_cents"], "limit_price_cents")
            if (
                count != count.quantize(CONTRACT_STEP)
                or not 0 < limit < 100
                or limit != limit.quantize(Decimal("0.01"))
            ):
                raise ValueError(
                    f"Line {number}: count or limit exceeds Kalshi fixed-point precision"
                )
            signals.append({
                "market_ticker": ticker,
                "decision_ts": decision_ts,
                "side": side,
                "contracts": count,
                "limit_price_cents": limit,
            })
    if not signals:
        raise ValueError("Signals CSV is empty")
    return sorted(signals, key=lambda row: row["decision_ts"])


def walk_asks(book, side: str, count: Decimal, limit: Decimal, depth_fraction: Decimal):
    """Hypothetical IOC against displayed asks; return fills, cost, fee basis, levels."""
    opposing_bids = book.no if side == "yes" else book.yes
    filled = cost = fee_basis = Decimal("0")
    levels = []
    for bid, visible in sorted(opposing_bids.items(), reverse=True):
        ask = Decimal(str(round(100 - bid, 4)))
        if ask > limit:
            break
        available = (Decimal(str(visible)) * depth_fraction).quantize(
            CONTRACT_STEP, rounding=ROUND_DOWN
        )
        take = min(count - filled, available)
        if take <= 0:
            continue
        price = ask / 100
        filled += take
        cost += take * price
        fee_basis += take * price * (1 - price)
        levels.append({"price_cents": str(ask), "contracts": str(take)})
        if filled == count:
            break
    return filled, cost, fee_basis, levels


def _capture_rows(path: Path):
    last_ns = 0
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            received_ns = row["received_ns"]
            if not isinstance(received_ns, int) or received_ns < last_ns:
                raise ValueError("Capture receipt times must be monotonic nanoseconds")
            last_ns = received_ns
            yield row


def _summary(rows: list[dict]) -> dict:
    filled = [row for row in rows if Decimal(row["filled_contracts"]) > 0]
    requested = sum((Decimal(row["contracts"]) for row in rows), Decimal(0))
    quantity = sum((Decimal(row["filled_contracts"]) for row in rows), Decimal(0))
    return {
        "signals": len(rows),
        "filled_orders": len(filled),
        "order_fill_rate": len(filled) / len(rows) if rows else None,
        "filled_contracts": str(quantity),
        "fill_fraction": float(quantity / requested) if requested else None,
        "net_pnl_dollars": str(
            sum((Decimal(row["pnl_dollars"]) for row in rows), Decimal(0))
        ),
        "statuses": dict(Counter(row["status"] for row in rows)),
    }


def run_backtest(
    *, input_dir: Path, capture_path: Path, signals_path: Path,
    output_dir: Path, latency_ms: int = 100, max_book_age_ms: int = 2000,
    depth_fraction: Decimal = Decimal("1"),
    fee_multiplier: Decimal = Decimal("1"),
    balance_precision: Decimal = Decimal("0.01"),
    holdout_fraction: float = 0.2,
) -> dict:
    if (
        latency_ms < 0
        or max_book_age_ms < 0
        or not depth_fraction.is_finite()
        or not 0 < depth_fraction <= 1
        or not fee_multiplier.is_finite()
        or fee_multiplier < 0
        or balance_precision not in {Decimal("0.01"), Decimal("0.0001")}
        or not 0 < holdout_fraction < 1
    ):
        raise ValueError(
            "Invalid latency, book age, depth fraction, fee multiplier, or holdout fraction"
        )
    source = json.loads((input_dir / "source.json").read_text())
    markets = {
        market["ticker"]: market
        for market in source["markets"]
        if market.get("result") in {"yes", "no"}
    }
    signals = load_signals(signals_path, markets)
    closes = {parse_iso8601_to_epoch(m.get("close_time")) for m in markets.values()}
    if None in closes or not closes:
        raise ValueError("Market close time is missing")
    closes = sorted(closes)
    boundary = (
        closes[max(1, min(len(closes) - 1, int(len(closes) * (1 - holdout_fraction))))]
        if len(closes) > 1 else float("inf")
    )

    replay = BookReplay()
    rows = _capture_rows(capture_path)
    upcoming = next(rows, None)
    results = []
    for signal in signals:
        arrival_ns = round(signal["decision_ts"] * 1e9) + latency_ms * 1_000_000
        while upcoming is not None and upcoming["received_ns"] <= arrival_ns:
            replay.ingest(upcoming)
            upcoming = next(rows, None)
        ticker = signal["market_ticker"]
        market = markets[ticker]
        close_ts = parse_iso8601_to_epoch(market["close_time"])
        open_ts = parse_iso8601_to_epoch(market.get("open_time"))
        book = replay.books.get(ticker)
        book_ns = replay.updated_ns.get(ticker)
        age_ms = (arrival_ns - book_ns) / 1e6 if book_ns is not None else None
        status = "filled"
        if (
            (open_ts is not None and signal["decision_ts"] < open_ts)
            or signal["decision_ts"] >= close_ts
            or arrival_ns >= round(close_ts * 1e9)
        ):
            status = "outside_market_hours"
        elif book is None:
            status = "no_continuous_book"
        elif age_ms is None or age_ms > max_book_age_ms:
            status = "stale_book"
        filled = cost = basis = Decimal("0")
        levels = []
        if status == "filled":
            filled, cost, basis, levels = walk_asks(
                book, signal["side"], signal["contracts"],
                signal["limit_price_cents"], depth_fraction,
            )
            if filled == 0:
                status = "no_depth_within_limit"
            elif filled < signal["contracts"]:
                status = "partial"
        trade_fee = (
            (TAKER_COEFFICIENT * fee_multiplier * basis).quantize(
                MICRODOLLAR, rounding=ROUND_CEILING
            ) if filled else Decimal("0")
        )
        # Aggregate levels as one order. Exact exchange rounding also depends on
        # individual matches, which aggregated public depth cannot reconstruct.
        fee = (
            (cost + trade_fee).quantize(balance_precision, rounding=ROUND_CEILING)
            - cost if filled else Decimal("0")
        )
        payout = filled if market["result"] == signal["side"] else Decimal("0")
        result = {
            **{
                key: str(value) if isinstance(value, Decimal) else value
                for key, value in signal.items()
            },
            "arrival_ts": arrival_ns / 1e9,
            "close_ts": close_ts,
            "split": "train" if close_ts < boundary else "holdout",
            "result": market["result"],
            "book_age_ms": age_ms,
            "status": status,
            "filled_contracts": str(filled),
            "avg_fill_price_cents": str(cost / filled * 100) if filled else "",
            "cost_dollars": str(cost),
            "fee_dollars": str(fee),
            "payout_dollars": str(payout),
            "pnl_dollars": str(payout - cost - fee),
            "levels": json.dumps(levels, separators=(",", ":")),
        }
        results.append(result)

    output_dir.mkdir(parents=True, exist_ok=True)
    with (output_dir / "fills.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(results[0]))
        writer.writeheader()
        writer.writerows(results)
    summary = {
        "model": "single-entry IOC-like taker, held to settlement",
        "capture_path": str(capture_path),
        "signals_path": str(signals_path),
        "latency_ms": latency_ms,
        "max_book_age_ms": max_book_age_ms,
        "depth_fraction": str(depth_fraction),
        "fee_multiplier": str(fee_multiplier),
        "balance_precision_dollars": str(balance_precision),
        "fee_schedule": "Kalshi July 7 2026 general taker formula; aggregated-order rounding estimate",
        "all": _summary(results),
        "train": _summary([row for row in results if row["split"] == "train"]),
        "holdout": _summary([row for row in results if row["split"] == "holdout"]),
    }
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--capture-path", type=Path, required=True)
    parser.add_argument("--signals", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--latency-ms", type=int, default=100)
    parser.add_argument("--max-book-age-ms", type=int, default=2000)
    parser.add_argument("--depth-fraction", type=Decimal, default=Decimal("1"))
    parser.add_argument("--fee-multiplier", type=Decimal, default=Decimal("1"))
    parser.add_argument("--balance-precision", type=Decimal, default=Decimal("0.01"))
    parser.add_argument("--holdout-fraction", type=float, default=0.2)
    args = parser.parse_args()
    print(json.dumps(run_backtest(
        input_dir=args.input_dir,
        capture_path=args.capture_path,
        signals_path=args.signals,
        output_dir=args.output_dir,
        latency_ms=args.latency_ms,
        max_book_age_ms=args.max_book_age_ms,
        depth_fraction=args.depth_fraction,
        fee_multiplier=args.fee_multiplier,
        balance_precision=args.balance_precision,
        holdout_fraction=args.holdout_fraction,
    ), indent=2))


if __name__ == "__main__":
    main()
