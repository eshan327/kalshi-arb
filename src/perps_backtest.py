"""Replay external, non-overlapping Perps round trips against captured L2 depth."""

from __future__ import annotations

import argparse
import csv
import json
import math
from datetime import datetime, time, timedelta
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from pathlib import Path
from zoneinfo import ZoneInfo

from core.markets import parse_iso8601_to_epoch
from data.capture import iter_capture


def number(value, *, positive=False) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError) as exc:
        raise ValueError(f"Invalid decimal: {value!r}") from exc
    if not result.is_finite() or (positive and result <= 0):
        raise ValueError(f"Invalid decimal: {value!r}")
    return result


class Book:
    def __init__(self, ticker: str):
        self.ticker = ticker
        self.session = None
        self.sid = None
        self.seq = None
        self.updated_ns = None
        self.bids: dict[Decimal, Decimal] = {}
        self.asks: dict[Decimal, Decimal] = {}

    def clear(self) -> None:
        self.sid = self.seq = self.updated_ns = None
        self.bids.clear()
        self.asks.clear()

    def ingest(self, row: dict) -> None:
        if row["session"] != self.session:
            self.clear()
            self.session = row["session"]
        data = row["data"]
        kind = data.get("type")
        if kind == "session_end":
            self.clear()
            return
        if kind not in {"orderbook_snapshot", "orderbook_delta"}:
            return
        msg = data.get("msg") or {}
        if msg.get("market_ticker") != self.ticker:
            return
        sid, seq = data.get("sid"), data.get("seq")
        if type(sid) is not int or type(seq) is not int:
            self.clear()
            return
        if kind == "orderbook_snapshot":
            if sid == self.sid and self.seq is not None and seq <= self.seq:
                return
            self.clear()
            self.sid, self.seq = sid, seq
            for field, book in (("bid", self.bids), ("ask", self.asks)):
                for raw_price, raw_quantity in msg[field]:
                    price, quantity = number(raw_price, positive=True), number(raw_quantity, positive=True)
                    if price in book:
                        raise ValueError("Duplicate Perps book level")
                    book[price] = quantity
        else:
            if self.sid is None or sid != self.sid:
                return
            if seq <= self.seq:
                return
            if seq != self.seq + 1:
                self.clear()
                return
            side = msg.get("side")
            if side not in {"bid", "ask"}:
                self.clear()
                return
            price, delta = number(msg.get("price"), positive=True), number(msg.get("delta"))
            book = self.bids if side == "bid" else self.asks
            quantity = book.get(price, Decimal(0)) + delta
            if quantity < 0:
                self.clear()
                return
            if quantity:
                book[price] = quantity
            else:
                book.pop(price, None)
            self.seq = seq
        if self.bids and self.asks and max(self.bids) >= min(self.asks):
            self.clear()
            return
        self.updated_ns = row["received_ns"]

    def walk(self, side: str, quantity: Decimal, limit: Decimal,
             fraction: Decimal) -> tuple[Decimal, Decimal]:
        levels = self.asks if side == "buy" else self.bids
        filled = notional = Decimal(0)
        for price in sorted(levels, reverse=side == "sell"):
            if (side == "buy" and price > limit) or (side == "sell" and price < limit):
                break
            available = (levels[price] * fraction).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
            take = min(quantity - filled, available)
            filled += take
            notional += take * price
            if filled >= quantity:
                break
        return filled, notional


def _signals(path: Path, ticker: str) -> list[dict]:
    required = {"ticker", "entry_ts", "exit_ts", "side", "contracts",
                "entry_limit_dollars", "exit_limit_dollars"}
    with path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            raise ValueError(f"Signals need columns: {', '.join(sorted(required))}")
        rows = []
        for row in reader:
            entry, exit_ = float(row["entry_ts"]), float(row["exit_ts"])
            qty = number(row["contracts"], positive=True)
            if (row["ticker"] != ticker or row["side"] not in {"long", "short"}
                or not math.isfinite(entry) or not math.isfinite(exit_)
                or entry <= 0 or exit_ <= entry or qty != qty.quantize(Decimal("0.01"))):
                raise ValueError("Invalid Perps signal")
            rows.append({"ticker": ticker, "entry_ts": entry, "exit_ts": exit_,
                         "side": row["side"], "contracts": qty,
                         "entry_limit": number(row["entry_limit_dollars"], positive=True),
                         "exit_limit": number(row["exit_limit_dollars"], positive=True)})
    rows.sort(key=lambda row: row["entry_ts"])
    if not rows or any(a["exit_ts"] >= b["entry_ts"] for a, b in zip(rows, rows[1:])):
        raise ValueError("Signals must be non-empty, ordered, and non-overlapping")
    return rows


def _missing_funding(entry: float, exit_: float, asset_class: str,
                     event_times: set[float]) -> bool:
    hours = {"Crypto": (0, 8, 16), "Metals": (10,)}.get(asset_class)
    if hours is None:
        raise ValueError(f"Unknown Perps funding schedule: {asset_class!r}")
    eastern = ZoneInfo("America/New_York")
    day = datetime.fromtimestamp(entry, eastern).date()
    last_day = datetime.fromtimestamp(exit_, eastern).date()
    while day <= last_day:
        for hour in hours:
            timestamp = datetime.combine(day, time(hour), eastern).timestamp()
            if entry < timestamp <= exit_ and not any(abs(timestamp - actual) <= 1 for actual in event_times):
                return True
        day += timedelta(days=1)
    return False


def run_backtest(*, history_dir: Path, capture_path: Path, signals_path: Path,
                 output_dir: Path, latency_ms: int, max_book_age_ms: int,
                 depth_fraction: Decimal, taker_fee_rate: Decimal) -> dict:
    if (latency_ms < 0 or max_book_age_ms < 0
        or not depth_fraction.is_finite() or not 0 < depth_fraction <= 1
        or not taker_fee_rate.is_finite() or not 0 <= taker_fee_rate < 1):
        raise ValueError("Invalid replay assumptions")
    manifest = json.loads((history_dir / "manifest.json").read_text())
    if manifest.get("environment") != "prod":
        raise ValueError("Perps replay needs a production historical export")
    ticker = manifest["ticker"]
    asset_class = manifest["market"]["asset_class"]
    signals = _signals(signals_path, ticker)
    tick_size = number(manifest["market"]["tick_size"], positive=True)
    if any(row[key] % tick_size for row in signals for key in ("entry_limit", "exit_limit")):
        raise ValueError("Perps limits must use the market tick size")
    first = parse_iso8601_to_epoch(manifest["start"])
    last = parse_iso8601_to_epoch(manifest["end"])
    if any(row["entry_ts"] < first or row["exit_ts"] + latency_ms / 1000 > last for row in signals):
        raise ValueError("Signals outside exported funding-history bounds")
    funding = [json.loads(line) for line in (history_dir / "funding_rates.jsonl").read_text().splitlines()]
    events = []
    for row in funding:
        timestamp = parse_iso8601_to_epoch(row.get("funding_time"))
        rate = number(row.get("funding_rate"))
        mark = number(row.get("mark_price"), positive=True)
        if timestamp is None or abs(rate) > Decimal("0.02") or row.get("market_ticker") != ticker:
            raise ValueError("Invalid historical funding event")
        events.append((timestamp, rate, mark))
    events.sort()
    event_times = {event[0] for event in events}
    capture = iter_capture(capture_path)
    upcoming = next(capture, None)
    book = Book(ticker)

    def quote(ts: float, side: str, qty: Decimal, limit: Decimal):
        nonlocal upcoming
        arrival = round(ts * 1e9) + latency_ms * 1_000_000
        while upcoming is not None and upcoming["received_ns"] <= arrival:
            book.ingest(upcoming)
            upcoming = next(capture, None)
        if book.updated_ns is None:
            return "no_continuous_book", None
        if arrival - book.updated_ns > max_book_age_ms * 1_000_000:
            return "stale_book", None
        filled, notional = book.walk(side, qty, limit, depth_fraction)
        return ("filled", notional) if filled == qty else ("insufficient_depth", None)

    results = []
    position_open = False
    for signal in signals:
        qty = signal["contracts"]
        sign = 1 if signal["side"] == "long" else -1
        entry_side = "buy" if sign > 0 else "sell"
        exit_side = "sell" if sign > 0 else "buy"
        entry = exit_notional = None
        if position_open:
            status = "blocked_by_open_position"
        else:
            status, entry = quote(signal["entry_ts"], entry_side, qty, signal["entry_limit"])
            if status == "filled":
                status, exit_notional = quote(signal["exit_ts"], exit_side, qty, signal["exit_limit"])
                if status != "filled":
                    status = "exit_" + status
                    position_open = True
        entry_arrival = signal["entry_ts"] + latency_ms / 1000
        exit_arrival = signal["exit_ts"] + latency_ms / 1000
        if status == "filled" and _missing_funding(entry_arrival, exit_arrival, asset_class, event_times):
            status = "funding_history_incomplete"
        funding_cash = Decimal(0)
        if status == "filled":
            for ts, rate, mark in events:
                if entry_arrival < ts <= exit_arrival:
                    funding_cash -= sign * qty * mark * rate
            fees = (entry + exit_notional) * taker_fee_rate
            pnl = sign * (exit_notional - entry) + funding_cash - fees
        else:
            fees = pnl = None
        results.append({
            **{key: str(value) if isinstance(value, Decimal) else value for key, value in signal.items()},
            "status": status, "entry_notional_dollars": str(entry) if entry is not None else "",
            "exit_arrival_ts": exit_arrival,
            "exit_notional_dollars": str(exit_notional) if exit_notional is not None else "",
            "open_contracts": str(qty) if entry is not None and status.startswith("exit_") else "0",
            "funding_dollars": str(funding_cash) if status == "filled" else "",
            "fees_dollars": str(fees) if fees is not None else "",
            "net_pnl_dollars": str(pnl) if pnl is not None else "",
        })
    output_dir.mkdir(parents=True, exist_ok=False)
    with (output_dir / "round_trips.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(results[0]))
        writer.writeheader()
        writer.writerows(results)
    pnl_values = [number(row["net_pnl_dollars"]) for row in results if row["status"] == "filled"]
    summary = {"ticker": ticker, "signals": len(results), "completed_round_trips": len(pnl_values),
               "open_positions_at_signal_exit": sum(row["status"].startswith("exit_") for row in results),
               "net_pnl_dollars": str(sum(pnl_values, Decimal(0))),
               "latency_ms": latency_ms, "max_book_age_ms": max_book_age_ms,
               "depth_fraction": str(depth_fraction), "taker_fee_rate": str(taker_fee_rate),
               "note": "Displayed depth is hypothetical, funding uses published rates and marks; no liquidation or capital model."}
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history-dir", type=Path, required=True)
    parser.add_argument("--capture-path", type=Path, required=True)
    parser.add_argument("--signals", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--latency-ms", type=int, default=100)
    parser.add_argument("--max-book-age-ms", type=int, default=2000)
    parser.add_argument("--depth-fraction", type=Decimal, default=Decimal("0.5"))
    parser.add_argument("--taker-fee-rate", type=Decimal, required=True)
    args = parser.parse_args()
    print(json.dumps(run_backtest(
        history_dir=args.history_dir, capture_path=args.capture_path,
        signals_path=args.signals, output_dir=args.output_dir,
        latency_ms=args.latency_ms, max_book_age_ms=args.max_book_age_ms,
        depth_fraction=args.depth_fraction, taker_fee_rate=args.taker_fee_rate,
    ), indent=2))


if __name__ == "__main__":
    main()
