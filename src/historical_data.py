"""Bounded, reproducible Kalshi 15-minute and Perps public-data exports."""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from core.config import KALSHI_ENV
from core.markets import get_market_profile, parse_iso8601_to_epoch
from data.kalshi_perps import get_perps, get_perps_pages
from data.kalshi_rest import (
    get_historical_cutoff, get_market_candlesticks, get_market_trades,
    get_settled_markets,
)

DAY = 86400


def _bounds(start: str, end: str) -> tuple[int, int]:
    try:
        a = datetime.fromisoformat(start.replace("Z", "+00:00"))
        b = datetime.fromisoformat(end.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Use ISO 8601 timestamps with UTC offsets") from exc
    if a.tzinfo is None or b.tzinfo is None:
        raise ValueError("Time bounds need UTC offsets")
    first, last = a.timestamp(), b.timestamp()
    if not first.is_integer() or not last.is_integer() or first <= 0 or last <= first or last > datetime.now(UTC).timestamp():
        raise ValueError("Bounds must be whole past seconds with start before end")
    return int(first), int(last)


def _save(path: Path, value) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n")


def export_15m(asset: str, start: str, end: str, output_dir: Path) -> dict:
    if KALSHI_ENV != "prod":
        raise RuntimeError("Historical exports require KALSHI_ENV=prod")
    profile = get_market_profile(asset)
    first, last = _bounds(start, end)
    cutoffs = get_historical_cutoff()
    trades_cutoff = parse_iso8601_to_epoch(cutoffs.get("trades_created_ts"))
    if trades_cutoff is None:
        raise ValueError("Kalshi trades cutoff missing")
    markets_cutoff = parse_iso8601_to_epoch(cutoffs.get("market_settled_ts"))
    markets = [m for m in get_settled_markets(
        profile.kalshi_series_ticker, min_close_ts=first,
        archival_cutoff_ts=markets_cutoff,
    ) if m.get("result") in {"yes", "no"} and
        (close := parse_iso8601_to_epoch(m.get("close_time"))) is not None and
        first <= close < last]
    markets.sort(key=lambda m: (m["close_time"], m["ticker"]))
    if not markets:
        raise ValueError("No settled 15-minute markets in the requested period")
    output_dir.mkdir(parents=True, exist_ok=False)
    count = candle_count = missing_candles = 0
    with (output_dir / "trades.jsonl").open("w") as handle, (output_dir / "candles.jsonl").open("w") as candles_file:
        for index, market in enumerate(markets, 1):
            close = parse_iso8601_to_epoch(market["close_time"])
            opened = parse_iso8601_to_epoch(market.get("open_time"))
            if opened is None or opened >= close:
                raise ValueError(f"Missing or invalid open time: {market['ticker']}")
            settled = parse_iso8601_to_epoch(market.get("settlement_ts"))
            if settled is None:
                raise ValueError(f"Missing settlement time: {market['ticker']}")
            candles = get_market_candlesticks(
                profile.kalshi_series_ticker, market["ticker"],
                start_ts=math.ceil(opened), end_ts=math.ceil(close),
                archived=markets_cutoff is not None and settled < markets_cutoff,
            )
            seen_candles = set()
            for candle in candles:
                ts = candle.get("end_period_ts")
                if (type(ts) is not int or not opened < ts <= close or ts in seen_candles):
                    raise ValueError(f"Invalid minute candle for {market['ticker']}")
                seen_candles.add(ts)
                candles_file.write(json.dumps({"market_ticker": market["ticker"],
                                               "candle": candle}, separators=(",", ":")) + "\n")
                candle_count += 1
            expected = set(range((math.floor(opened) // 60 + 1) * 60,
                                 math.floor(close) + 1, 60))
            missing_candles += len(expected - seen_candles)
            trade_rows = get_market_trades(
                ticker=market["ticker"], min_ts=int(opened) - 1,
                max_ts=math.ceil(close), cutoff_ts=trades_cutoff,
            )
            for trade in trade_rows:
                ts = parse_iso8601_to_epoch(trade.get("created_time"))
                if (trade.get("ticker") != market["ticker"] or ts is None
                    or not opened <= ts <= close or trade.get("is_block_trade")):
                    raise ValueError(f"Invalid public trade for {market['ticker']}")
            for trade in sorted(trade_rows, key=lambda row: (
                parse_iso8601_to_epoch(row["created_time"]), row["trade_id"]
            )):
                handle.write(json.dumps({"market_ticker": market["ticker"],
                                         "trade": trade}, separators=(",", ":")) + "\n")
                count += 1
            print(f"Public trades: {index}/{len(markets)} markets", file=sys.stderr, flush=True)
    source = {
        "kind": "market-only", "asset": profile.asset,
        "series_ticker": profile.kalshi_series_ticker,
        "start_close": start, "end_close": end,
        "retrieved_at": datetime.now(UTC).isoformat(), "environment": KALSHI_ENV,
        "settlement_source": "Pyth 1-minute close" if not profile.index_id else "CF Benchmarks 60-second average",
        "historical_cutoff": cutoffs, "markets": markets,
        "trades_file": "trades.jsonl", "candles_file": "candles.jsonl",
    }
    _save(output_dir / "source.json", source)
    summary = {"markets": len(markets), "trades": count, "minute_candles": candle_count,
               "missing_minute_candles": missing_candles,
               "source": str(output_dir / "source.json")}
    _save(output_dir / "summary.json", summary)
    return summary


def _unique(rows: list[dict], key: str) -> list[dict]:
    found = {}
    for row in rows:
        value = row.get(key)
        if value is None:
            raise ValueError(f"Missing {key} in historical data")
        if value in found and found[value] != row:
            raise ValueError(f"Conflicting {key}={value} in historical data")
        found[value] = row
    return [found[value] for value in sorted(found)]


def _price(value) -> Decimal | None:
    if value is None:
        return None
    try:
        price = Decimal(str(value))
    except (InvalidOperation, TypeError) as exc:
        raise ValueError("Invalid historical price") from exc
    if not price.is_finite() or price <= 0:
        raise ValueError("Invalid historical price")
    return price


def export_perps(ticker: str, start: str, end: str, output_dir: Path) -> dict:
    if KALSHI_ENV != "prod":
        raise RuntimeError("Historical exports require KALSHI_ENV=prod")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", ticker):
        raise ValueError("Invalid Perps ticker")
    first, last = _bounds(start, end)
    market = get_perps(f"/markets/{ticker}")["market"]
    if market.get("ticker") != ticker:
        raise ValueError("Perps market ticker mismatch")
    candles, trades, funding = [], [], []
    # Small windows avoid undocumented response-size ceilings and bound memory.
    for left in range(first, last, DAY):
        right = min(left + DAY, last)
        page = get_perps(f"/markets/{ticker}/candlesticks", start_ts=left,
                         end_ts=right, period_interval=1)
        if page.get("ticker") != ticker:
            raise ValueError("Perps candlestick ticker mismatch")
        candles.extend(page["candlesticks"])
        trades.extend(get_perps_pages("/trades", "trades", ticker=ticker,
                                     min_ts=left - 1, max_ts=right))
        funding.extend(get_perps("/funding_rates/historical", ticker=ticker,
                                 start_ts=left, end_ts=right)["funding_rates"])
    candles = _unique(candles, "end_period_ts")
    trades = _unique(trades, "trade_id")
    funding = _unique(funding, "funding_time")
    for row in candles:
        if type(row["end_period_ts"]) is not int or row["end_period_ts"] % 60:
            raise ValueError("Invalid Perps candle timestamp")
        bid = _price((row.get("bid") or {}).get("close"))
        ask = _price((row.get("ask") or {}).get("close"))
        if bid is not None and ask is not None and bid > ask:
            raise ValueError("Crossed Perps candle close")
    if any(parse_iso8601_to_epoch(row.get("created_time")) is None for row in trades):
        raise ValueError("Perps trade has invalid timestamp")
    if any(parse_iso8601_to_epoch(row.get("funding_time")) is None for row in funding):
        raise ValueError("Perps funding event has invalid timestamp")
    for row in funding:
        try:
            rate = Decimal(str(row["funding_rate"]))
        except (KeyError, InvalidOperation) as exc:
            raise ValueError("Invalid Perps funding rate") from exc
        if not rate.is_finite() or _price(row.get("mark_price")) is None:
            raise ValueError("Invalid Perps funding event")
    candles = [row for row in candles if first < row["end_period_ts"] <= last]
    trades = [row for row in trades if first <= parse_iso8601_to_epoch(row["created_time"]) < last]
    funding = [row for row in funding if first < parse_iso8601_to_epoch(row["funding_time"]) <= last]
    trades.sort(key=lambda row: (parse_iso8601_to_epoch(row["created_time"]), row["trade_id"]))
    funding.sort(key=lambda row: parse_iso8601_to_epoch(row["funding_time"]))
    if any(row.get("ticker") != ticker for row in trades) or any(row.get("market_ticker") != ticker for row in funding):
        raise ValueError("Cross-market Perps history returned")
    if not candles:
        raise ValueError("No Perps candles in requested period")
    observed = {row["end_period_ts"] for row in candles}
    expected = set(range((first // 60 + 1) * 60, last + 1, 60))
    output_dir.mkdir(parents=True, exist_ok=False)
    for name, rows in (("candles", candles), ("trades", trades), ("funding_rates", funding)):
        with (output_dir / f"{name}.jsonl").open("w") as handle:
            for row in rows:
                handle.write(json.dumps(row, separators=(",", ":")) + "\n")
    manifest = {"ticker": ticker, "start": start, "end": end,
                "environment": KALSHI_ENV,
                "retrieved_at": datetime.now(UTC).isoformat(), "market": market,
                "candles": len(candles), "trades": len(trades),
                "funding_events": len(funding), "missing_minute_candles": len(expected - observed),
                "note": "Candles and trades are research data, not historical executable depth."}
    _save(output_dir / "manifest.json", manifest)
    return {key: manifest[key] for key in ("ticker", "candles", "trades", "funding_events", "missing_minute_candles")}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("product", choices=("15m", "perps"))
    parser.add_argument("asset_or_ticker")
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    export = export_15m if args.product == "15m" else export_perps
    print(json.dumps(export(args.asset_or_ticker, args.start, args.end, args.output_dir), indent=2))


if __name__ == "__main__":
    main()
