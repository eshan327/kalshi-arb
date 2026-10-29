"""Fixed-horizon baseline datasets and chronological evaluation; no simulated fills."""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from backtesting.cf import (
    _hour_start,
    _iso_hour,
    _latest_tick,
    _parse_timestamp,
    _settlement_state,
    _ticks_between,
    fetch_cf_feeds,
    fetch_cf_range,
    normalize_cf_history,
    one_second_boundary_ticks,
)
from backtesting.model import (
    DEFAULT_HORIZONS_SECONDS,
    ModelObservation,
    evaluate_market,
    pricing_at,
)
from backtesting.scoring import chronological_split, score_predictions, summarize
from core.config import KALSHI_ENV, WS_BASE_URL
from core.markets import get_market_profile, parse_iso8601_to_epoch
from data.capture import load_quote_tapes
from data.kalshi_rest import get_historical_cutoff, get_market_trades, get_settled_markets
from pricing.vol_estimator import BASELINE_VOL_WINDOW_SECONDS

CF_HISTORY_DATA_LAG_BUFFER_SEC = 20 * 60


def run_backtest(
    *,
    asset: str,
    max_markets: int = 100,
    horizons: tuple[int, ...] = DEFAULT_HORIZONS_SECONDS,
    output_dir: Path = Path("output/baseline"),
    input_dir: Path | None = None,
    capture_path: Path | None = None,
    holdout_fraction: float = 0.2,
    max_trade_age_seconds: float = 5.0,
    vol_window_seconds: float = BASELINE_VOL_WINDOW_SECONDS,
    start_close: str | None = None,
    end_close: str | None = None,
) -> dict:
    """Save source data once; --input-dir replays the same experiment without API reads."""
    profile = get_market_profile(asset)
    if not profile.index_id:
        raise ValueError("Pyth-settled metals have no CF baseline; export with historical_data.py")
    if not horizons or any(not isinstance(h, int) or h <= 0 for h in horizons):
        raise ValueError("Positive horizons required")
    if not 0 < holdout_fraction < 1 or max_markets < 0:
        raise ValueError("Invalid holdout fraction or market count")
    if not math.isfinite(max_trade_age_seconds) or max_trade_age_seconds < 0:
        raise ValueError("Invalid maximum trade age")
    if not math.isfinite(vol_window_seconds) or vol_window_seconds < 3:
        raise ValueError("Volatility window must be at least three seconds")
    start_ts = parse_iso8601_to_epoch(start_close) if start_close else None
    end_ts = parse_iso8601_to_epoch(end_close) if end_close else None
    if (start_close and start_ts is None) or (end_close and end_ts is None):
        raise ValueError("Close-time bounds must be ISO 8601 timestamps")
    if start_ts is not None and end_ts is not None and start_ts >= end_ts:
        raise ValueError("Start close must precede end close")

    def in_period(market: dict) -> bool:
        close = parse_iso8601_to_epoch(market.get("close_time"))
        return close is not None and (start_ts is None or close >= start_ts) and (
            end_ts is None or close < end_ts
        )

    output_dir.mkdir(parents=True, exist_ok=False)
    if input_dir is not None:
        if capture_path is not None:
            raise ValueError("Offline replay uses quotes already saved in source.json")
        source = json.loads((input_dir / "source.json").read_text())
        if source.get("kind") == "market-only":
            raise ValueError("Market-only export has no CF ticks; use taker_backtest.py")
        if source["asset"] != profile.asset:
            raise ValueError("Source asset does not match requested asset")
        markets = [market for market in source["markets"] if in_period(market)]
        trades = {m["ticker"]: source["trades"].get(m["ticker"], []) for m in markets}
        quotes = {m["ticker"]: source.get("quotes", {}).get(m["ticker"], []) for m in markets}
        if any(quotes.values()) and (source.get("environment") != "prod" or not source.get("ws_endpoint")):
            raise ValueError("Saved quotes need production capture provenance; export the baseline again")
        source = {**source, "markets": markets, "trades": trades, "quotes": quotes}
        with (input_dir / "cf_ticks.csv").open(newline="") as handle:
            spot_ticks = [
                {"ts": float(r["ts"]), "price": float(r["price"])}
                for r in csv.DictReader(handle)
            ]
        if any(
            not math.isfinite(r["ts"])
            or not math.isfinite(r["price"])
            or r["price"] <= 0
            for r in spot_ticks
        ):
            raise ValueError("Invalid saved CF data")
        spot_ticks.sort(key=lambda r: r["ts"])
        fix_ticks = one_second_boundary_ticks(spot_ticks)
    else:
        if KALSHI_ENV != "prod":
            raise RuntimeError("Historical research requires KALSHI_ENV=prod")
        cutoffs = get_historical_cutoff()
        trades_cutoff = parse_iso8601_to_epoch(cutoffs.get("trades_created_ts"))
        markets_cutoff = parse_iso8601_to_epoch(cutoffs.get("market_settled_ts"))
        if trades_cutoff is None:
            raise ValueError("Kalshi did not return a valid trades cutoff")
        cutoff = time.time() - CF_HISTORY_DATA_LAG_BUFFER_SEC
        markets = [
            m
            for m in get_settled_markets(
                profile.kalshi_series_ticker,
                min_close_ts=start_ts,
                archival_cutoff_ts=markets_cutoff,
            )
            if m.get("result") in {"yes", "no"}
            and in_period(m)
            and (parse_iso8601_to_epoch(m.get("close_time")) or float("inf")) <= cutoff
        ]
        markets.sort(key=lambda m: parse_iso8601_to_epoch(m["close_time"]))
        if max_markets:
            markets = markets[-max_markets:]
        if not markets:
            raise ValueError("No settled markets available")
        print(f"Exporting {len(markets)} {profile.asset} markets and CF history", file=sys.stderr, flush=True)
        closes = [parse_iso8601_to_epoch(m["close_time"]) for m in markets]
        spot_ticks, fix_ticks, resolution = fetch_cf_feeds(
            profile,
            min(closes) - max(horizons) - vol_window_seconds - 1,
            max(closes),
        )
        trades = {}
        for index, m in enumerate(markets, 1):
            trades[m["ticker"]] = get_market_trades(
                ticker=m["ticker"],
                min_ts=int(
                    parse_iso8601_to_epoch(m["close_time"])
                    - max(horizons)
                    - max_trade_age_seconds
                ),
                max_ts=int(parse_iso8601_to_epoch(m["close_time"])),
                cutoff_ts=trades_cutoff,
            )
            if index % 25 == 0 or index == len(markets):
                print(f"Public trades: {index}/{len(markets)} markets", file=sys.stderr, flush=True)
        quote_tapes = load_quote_tapes(capture_path, environment=KALSHI_ENV, endpoint=WS_BASE_URL) if capture_path else {}
        quotes = {m["ticker"]: quote_tapes.get(m["ticker"], []) for m in markets}
        source = {
            "asset": profile.asset,
            "environment": KALSHI_ENV,
            "ws_endpoint": WS_BASE_URL,
            "retrieved_at": datetime.now(UTC).isoformat(),
            "resolution": resolution,
            "historical_cutoff": cutoffs,
            "markets": markets,
            "trades": trades,
            "quotes": quotes,
            "quote_source": "local WebSocket receipt timestamps" if capture_path else None,
        }
    # Source exports are part of the dataset, not a second ingestion framework.
    (output_dir / "source.json").write_text(json.dumps(source, indent=2) + "\n")
    with (output_dir / "cf_ticks.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["ts", "price"])
        writer.writeheader()
        writer.writerows(spot_ticks)
    rows, failures = [], {}
    for market in markets:
        rows.extend(
            evaluate_market(
                market,
                asset=profile.asset,
                spot_ticks=spot_ticks,
                fix_ticks=fix_ticks,
                horizons=horizons,
                subsecond_offset=0.4 if profile.high_frequency else 0.0,
                trades=trades.get(market["ticker"], []),
                quotes=quotes.get(market["ticker"], []),
                max_trade_age_seconds=max_trade_age_seconds,
                vol_window_seconds=vol_window_seconds,
                failure_counts=failures,
            )
        )
    if not rows:
        raise ValueError(f"No valid baseline observations: {failures}")
    train, holdout, crossing = chronological_split(rows, holdout_fraction)
    with (output_dir / "observations.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=[*asdict(rows[0]), "split"])
        writer.writeheader()
        for name, bucket in (("train", train), ("holdout", holdout), ("crossing", crossing)):
            writer.writerows({**asdict(row), "split": name} for row in bucket)
    summary = {
        "baseline": "settlement-asian-v1",
        "asset": profile.asset,
        "vol_window_seconds": vol_window_seconds,
        "horizons": horizons,
        "holdout_fraction": holdout_fraction,
        "max_trade_age_seconds": max_trade_age_seconds,
        "start_close": start_close,
        "end_close": end_close,
        "attempted_markets": len(markets),
        "rejections": failures,
        "train": summarize(train),
        "holdout": summarize(holdout),
        "crossing": summarize(crossing),
        "note": "Trade prices are proxies. Captured quotes use local receipt times and sequence-checked snapshots/deltas, but do not prove available fills or profitability. No orders are simulated.",
    }
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("asset", nargs="?", default="BTC")
    parser.add_argument("--max-markets", type=int, default=100)
    parser.add_argument(
        "--horizons", default=",".join(map(str, DEFAULT_HORIZONS_SECONDS))
    )
    parser.add_argument("--holdout-fraction", type=float, default=0.2)
    parser.add_argument("--max-trade-age-seconds", type=float, default=5.0)
    parser.add_argument("--vol-window-seconds", type=float, default=BASELINE_VOL_WINDOW_SECONDS)
    parser.add_argument("--start-close", help="Include markets closing at or after this ISO 8601 time")
    parser.add_argument("--end-close", help="Exclude markets closing at or after this ISO 8601 time")
    parser.add_argument("--output-dir", type=Path, default=Path("output/baseline"))
    parser.add_argument("--input-dir", type=Path)
    parser.add_argument("--capture-path", type=Path)
    args = parser.parse_args()
    print(
        json.dumps(
            run_backtest(
                asset=args.asset,
                max_markets=args.max_markets,
                horizons=tuple(int(h) for h in args.horizons.split(",")),
                output_dir=args.output_dir,
                input_dir=args.input_dir,
                capture_path=args.capture_path,
                holdout_fraction=args.holdout_fraction,
                max_trade_age_seconds=args.max_trade_age_seconds,
                vol_window_seconds=args.vol_window_seconds,
                start_close=args.start_close,
                end_close=args.end_close,
            ),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
