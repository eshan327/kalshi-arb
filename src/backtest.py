"""Fixed-horizon baseline datasets and chronological evaluation; no simulated fills."""

from __future__ import annotations

import argparse
import csv
import json
import math
import time
from bisect import bisect_left, bisect_right
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.config import KALSHI_ENV
from core.markets import (
    MarketProfile,
    extract_settlement_decimals,
    extract_suggested_strike,
    get_market_profile,
    parse_iso8601_to_epoch,
)
from data.kalshi_rest import (
    get_cfbenchmarks_history,
    get_market_trades,
    get_settled_markets,
)
from pricing.baseline import compute_pricing_snapshot
from pricing.vol_estimator import BASELINE_VOL_WINDOW_SECONDS

DEFAULT_HORIZONS_SECONDS = (600, 300, 120, 90, 60, 45, 30, 20, 10, 5, 1)
CF_HISTORY_MIN_INTERVAL_SEC = 0.26
CF_HISTORY_DATA_LAG_BUFFER_SEC = 20 * 60


@dataclass(frozen=True)
class ModelObservation:
    market_ticker: str
    close_ts: float
    eval_ts: float
    nominal_horizon_seconds: int
    seconds_to_expiry: float
    actual_yes: int
    p_model: float
    spot: float
    strike: float
    model_strike: float
    settlement_decimals: int
    sigma_annual: float
    known_fix_count: int
    partial_average: float | None
    required_remaining_average: float | None
    regime: str
    p_market: float | None
    market_trade_ts: float | None
    market_trade_age_seconds: float | None


def _parse_timestamp(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e12:
            ts /= 1000.0
        return ts if math.isfinite(ts) and ts > 0 else None
    if isinstance(value, str):
        try:
            numeric = float(value)
        except ValueError:
            return parse_iso8601_to_epoch(value)
        return _parse_timestamp(numeric)
    return None


def normalize_cf_history(rows: list[dict]) -> list[dict[str, float]]:
    """Preserve published milliseconds; never floor subsecond spot into a fixing."""
    values = {}
    for row in rows:
        ts = _parse_timestamp(row.get("time"))
        price = float(row["value"])
        if ts is None or not math.isfinite(price) or price <= 0:
            raise ValueError("Invalid CF historical value")
        if ts in values and values[ts] != price:
            raise ValueError("Conflicting CF historical values")
        values[ts] = price
    return [{"ts": ts, "price": values[ts]} for ts in sorted(values)]


def one_second_boundary_ticks(
    ticks: list[dict[str, float]],
) -> list[dict[str, float]]:
    """Return only exact second-boundary CF values, matching cfbenchmarks_value."""
    return [tick for tick in ticks if float(tick["ts"]).is_integer()]


def _hour_start(ts: float) -> float:
    return float(int(ts) // 3600 * 3600)


def _iso_hour(ts: float) -> str:
    return (
        datetime.fromtimestamp(_hour_start(ts), UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def fetch_cf_range(
    index_id: str,
    start_ts: float,
    end_ts: float,
) -> list[dict[str, float]]:
    """Fetch a contiguous CF historical range at its published resolution."""
    raw: list[dict] = []
    cursor = _hour_start(start_ts)
    final_hour = _hour_start(end_ts)
    first = True
    while cursor <= final_hour:
        if not first:
            # Passthrough history reads cost 50 read tokens. Basic accounts have a
            # 200-token/sec budget, so pace bulk research at just under 4 req/sec.
            time.sleep(CF_HISTORY_MIN_INTERVAL_SEC)
        raw.extend(
            get_cfbenchmarks_history(
                index_id,
                timestamp=_iso_hour(cursor),
                timespan="HOUR",
            )
        )
        first = False
        cursor += 3600

    return [
        tick for tick in normalize_cf_history(raw) if start_ts <= tick["ts"] <= end_ts
    ]


def fetch_cf_feeds(
    profile: MarketProfile,
    start_ts: float,
    end_ts: float,
) -> tuple[list[dict[str, float]], list[dict[str, float]], str]:
    """
    Recreate the live 5 Hz/1 Hz split from CF's historical tick stream.

    Historical /history/values does not accept maxResolution. For subsecond RTIs it
    returns the published 200 ms ticks, including exact second-boundary values.
    The live bot uses all ticks only for current spot while the 1 Hz channel owns
    volatility history and settlement fixes, so replay filters those exact
    second-boundary publications explicitly.
    """
    published = fetch_cf_range(profile.index_id, start_ts, end_ts)
    if not published:
        raise ValueError("CF historical values are unavailable")

    fixes = one_second_boundary_ticks(published)
    if not fixes:
        raise ValueError("CF history contained no exact one-second boundary values")
    return published, fixes, "PER_200MS" if profile.high_frequency else "PER_SECOND"


def _ticks_between(
    ticks: list[dict[str, float]],
    start_ts: float,
    end_ts: float,
    *,
    start_inclusive: bool = True,
) -> list[dict[str, float]]:
    """Slice sorted ticks in O(log n + k) without rescanning the full history."""
    key = lambda row: row["ts"]
    left = (
        bisect_left(ticks, start_ts, key=key)
        if start_inclusive
        else bisect_right(ticks, start_ts, key=key)
    )
    right = bisect_right(ticks, end_ts, key=key)
    return ticks[left:right]


def _latest_tick(
    ticks: list[dict[str, float]], now_ts: float
) -> dict[str, float] | None:
    if not ticks:
        return None
    index = bisect_right(ticks, now_ts, key=lambda row: row["ts"])
    return None if index == 0 else ticks[index - 1]


def _settlement_state(
    fix_ticks: list[dict[str, float]],
    *,
    now_ts: float,
    close_ts: float,
    window: int,
    spot_ts: float | None = None,
) -> dict[str, Any]:
    latest_fix = _latest_tick(fix_ticks, now_ts)
    timestamp = (
        spot_ts
        if spot_ts is not None
        else (latest_fix["ts"] if latest_fix is not None else 0.0)
    )
    if latest_fix is None or timestamp <= 0:
        return {"connected": False, "timestamp": 0.0}

    state: dict[str, Any] = {
        "connected": True,
        "timestamp": float(timestamp),
    }
    start = close_ts - window
    if now_ts <= start:
        return state

    fixes = _ticks_between(
        fix_ticks,
        start,
        min(now_ts, close_ts),
        start_inclusive=False,
    )
    if not fixes:
        return state

    expected = list(range(int(start) + 1, math.floor(min(now_ts, close_ts)) + 1))
    if [tick["ts"] for tick in fixes] != expected:
        return state  # Missing fixes cannot be reconstructed from later spot values.

    mean = sum(tick["price"] for tick in fixes) / len(fixes)
    state.update(
        average_ts=latest_fix["ts"],
        final_average={
            "start": start,
            "end": fixes[-1]["ts"],
            "count": len(fixes),
            "value": mean,
        },
    )
    return state


def pricing_at(
    *,
    profile: MarketProfile,
    asset: str,
    market: dict,
    strike: float,
    decimals: int,
    eval_ts: float,
    spot_ticks: list[dict[str, float]],
    fix_ticks: list[dict[str, float]],
    vol_window_seconds: float = BASELINE_VOL_WINDOW_SECONDS,
) -> dict[str, Any]:
    latest_spot = _latest_tick(spot_ticks, eval_ts)
    latest_fix = _latest_tick(fix_ticks, eval_ts)
    if latest_spot is None or latest_fix is None:
        return {"ready": False, "reason": "missing_history"}

    state = _settlement_state(
        fix_ticks,
        now_ts=eval_ts,
        close_ts=parse_iso8601_to_epoch(market.get("close_time")) or 0.0,
        window=profile.settlement_window_seconds,
        spot_ts=latest_spot["ts"],
    )
    available_fixes = _ticks_between(
        fix_ticks,
        eval_ts - max(float(vol_window_seconds), 1.0),
        eval_ts,
    )
    snapshot = compute_pricing_snapshot(
        profile=profile,
        feed_asset=asset,
        spot=latest_spot["price"],
        ticks=available_fixes,
        strike=strike,
        market_ticker=str(market.get("ticker") or ""),
        close_time_iso=market.get("close_time"),
        settlement_decimals=decimals,
        index_state=state,
        now_ts=eval_ts,
        vol_window_seconds=vol_window_seconds,
    )
    return snapshot


def evaluate_market(
    market: dict,
    *,
    asset: str,
    spot_ticks: list[dict[str, float]],
    fix_ticks: list[dict[str, float]],
    horizons: tuple[int, ...] = DEFAULT_HORIZONS_SECONDS,
    subsecond_offset: float = 0.0,
    trades: list[dict] | None = None,
    max_trade_age_seconds: float = 5.0,
    vol_window_seconds: float = BASELINE_VOL_WINDOW_SECONDS,
    failure_counts: dict[str, int] | None = None,
) -> list[ModelObservation]:
    """Export raw baseline state; every input to a prediction is available by eval_ts.

    A recent trade is only a market-probability proxy, never an executable quote.
    Its timestamp and age remain visible; missing/stale comparisons stay null.
    """
    if (
        not 0 <= subsecond_offset < 1
        or not math.isfinite(max_trade_age_seconds)
        or max_trade_age_seconds < 0
    ):
        raise ValueError("Invalid observation offset or maximum trade age")
    if not horizons or any(not isinstance(h, int) or h <= 0 for h in horizons):
        raise ValueError("Horizons must be positive whole seconds")
    profile = get_market_profile(asset)
    ticker = str(market.get("ticker") or "")
    strike = extract_suggested_strike(market)
    close = parse_iso8601_to_epoch(market.get("close_time"))
    opened = parse_iso8601_to_epoch(market.get("open_time"))
    outcome = str(market.get("result") or "").lower()
    if not ticker or strike is None or close is None or outcome not in {"yes", "no"}:
        raise ValueError("A settled market with explicit terms is required")
    decimals = extract_settlement_decimals(market, profile.settlement_decimals_fallback)
    tape = []
    for trade in trades or []:
        ts = _parse_timestamp(trade.get("created_time"))
        try:
            probability = float(trade["yes_price_dollars"])
        except (KeyError, TypeError, ValueError):
            continue
        if ts is not None and 0 <= probability <= 1 and not trade.get("is_block_trade"):
            tape.append((ts, probability))
    tape.sort(key=lambda item: item[0])
    rows = []
    for horizon in sorted(set(horizons), reverse=True):
        now = close - horizon + subsecond_offset
        if opened is not None and now < opened:
            reason = "before_market_open"
            snapshot = {}
        else:
            snapshot = pricing_at(
                profile=profile,
                asset=profile.asset,
                market=market,
                strike=strike,
                decimals=decimals,
                eval_ts=now,
                spot_ticks=spot_ticks,
                fix_ticks=fix_ticks,
                vol_window_seconds=vol_window_seconds,
            )
            reason = snapshot.get("reason")
        if not snapshot.get("ready"):
            if failure_counts is not None:
                key = f"{horizon}s:{reason or 'not_ready'}"
                failure_counts[key] = failure_counts.get(key, 0) + 1
            continue
        index = bisect_right(tape, now, key=lambda item: item[0])
        trade_ts, p_market = tape[index - 1] if index else (None, None)
        age = None if trade_ts is None else now - trade_ts
        if age is not None and age > max_trade_age_seconds:
            trade_ts, p_market, age = None, None, None
        rows.append(
            ModelObservation(
                market_ticker=ticker,
                close_ts=close,
                eval_ts=now,
                nominal_horizon_seconds=horizon,
                seconds_to_expiry=close - now,
                actual_yes=int(outcome == "yes"),
                p_model=snapshot["p_model"],
                spot=snapshot["spot_index"],
                strike=strike,
                model_strike=snapshot["model_strike_usd"],
                settlement_decimals=decimals,
                sigma_annual=snapshot["sigma_annual"],
                known_fix_count=snapshot["twap_samples_observed"],
                partial_average=snapshot["twap_partial_avg_raw"],
                required_remaining_average=snapshot["twap_required_avg"],
                regime=snapshot["regime"],
                p_market=p_market,
                market_trade_ts=trade_ts,
                market_trade_age_seconds=age,
            )
        )
    return rows


def score_predictions(probabilities: list[float], outcomes: list[int]) -> dict:
    """The same scoring function can evaluate a future candidate on frozen rows."""
    if len(probabilities) != len(outcomes):
        raise ValueError("Predictions and outcomes must align")
    if any(not math.isfinite(p) or not 0 <= p <= 1 for p in probabilities) or any(
        y not in {0, 1} for y in outcomes
    ):
        raise ValueError("Invalid probability or binary outcome")
    n = len(outcomes)
    bounded = [min(1 - 1e-12, max(1e-12, p)) for p in probabilities]
    return {
        "observations": n,
        "brier": sum((p - y) ** 2 for p, y in zip(probabilities, outcomes)) / n
        if n
        else None,
        "log_loss": -sum(
            y * math.log(p) + (1 - y) * math.log(1 - p)
            for p, y in zip(bounded, outcomes)
        )
        / n
        if n
        else None,
    }


def chronological_split(rows: list[ModelObservation], holdout_fraction: float = 0.2):
    """Split whole close-time groups; all horizons of a market stay together."""
    if not 0 < holdout_fraction < 1:
        raise ValueError("Holdout fraction must be between zero and one")
    closes = sorted({row.close_ts for row in rows})
    if len(closes) < 2:
        raise ValueError("At least two market close times are required for a holdout")
    boundary = closes[
        max(1, min(len(closes) - 1, int(len(closes) * (1 - holdout_fraction))))
    ]
    ordered = sorted(rows, key=lambda row: (row.eval_ts, row.market_ticker))
    return [row for row in ordered if row.close_ts < boundary], [
        row for row in ordered if row.close_ts >= boundary
    ]


def summarize(rows: list[ModelObservation]) -> dict:
    def scores(bucket):
        paired = [row for row in bucket if row.p_market is not None]
        return {
            "baseline": score_predictions(
                [r.p_model for r in bucket], [r.actual_yes for r in bucket]
            ),
            "paired_baseline": score_predictions(
                [r.p_model for r in paired], [r.actual_yes for r in paired]
            ),
            "paired_market": score_predictions(
                [r.p_market for r in paired], [r.actual_yes for r in paired]
            ),
        }

    return {
        "markets": len({r.market_ticker for r in rows}),
        **scores(rows),
        "by_horizon": {
            str(h): scores([r for r in rows if r.nominal_horizon_seconds == h])
            for h in sorted({r.nominal_horizon_seconds for r in rows}, reverse=True)
        },
    }


def run_backtest(
    *,
    asset: str,
    max_markets: int = 100,
    horizons: tuple[int, ...] = DEFAULT_HORIZONS_SECONDS,
    output_dir: Path = Path("output/baseline"),
    input_dir: Path | None = None,
    holdout_fraction: float = 0.2,
    max_trade_age_seconds: float = 5.0,
) -> dict:
    """Save source data once; --input-dir replays the same experiment without API reads."""
    profile = get_market_profile(asset)
    if not horizons or any(not isinstance(h, int) or h <= 0 for h in horizons):
        raise ValueError("Positive horizons required")
    if not 0 < holdout_fraction < 1 or max_markets < 0:
        raise ValueError("Invalid holdout fraction or market count")
    if not math.isfinite(max_trade_age_seconds) or max_trade_age_seconds < 0:
        raise ValueError("Invalid maximum trade age")
    output_dir.mkdir(parents=True, exist_ok=True)
    if input_dir is not None:
        source = json.loads((input_dir / "source.json").read_text())
        if source["asset"] != profile.asset:
            raise ValueError("Source asset does not match requested asset")
        markets, trades = source["markets"], source["trades"]
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
        cutoff = time.time() - CF_HISTORY_DATA_LAG_BUFFER_SEC
        markets = [
            m
            for m in get_settled_markets(profile.kalshi_series_ticker)
            if m.get("result") in {"yes", "no"}
            and (parse_iso8601_to_epoch(m.get("close_time")) or float("inf")) <= cutoff
        ]
        markets.sort(key=lambda m: parse_iso8601_to_epoch(m["close_time"]))
        if max_markets:
            markets = markets[-max_markets:]
        if not markets:
            raise ValueError("No settled markets available")
        closes = [parse_iso8601_to_epoch(m["close_time"]) for m in markets]
        spot_ticks, fix_ticks, resolution = fetch_cf_feeds(
            profile,
            min(closes) - max(horizons) - BASELINE_VOL_WINDOW_SECONDS - 1,
            max(closes),
        )
        trades = {
            m["ticker"]: get_market_trades(
                ticker=m["ticker"],
                min_ts=int(
                    parse_iso8601_to_epoch(m["close_time"])
                    - max(horizons)
                    - max_trade_age_seconds
                ),
                max_ts=int(parse_iso8601_to_epoch(m["close_time"])),
            )
            for m in markets
        }
        source = {
            "asset": profile.asset,
            "retrieved_at": datetime.now(UTC).isoformat(),
            "resolution": resolution,
            "markets": markets,
            "trades": trades,
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
                max_trade_age_seconds=max_trade_age_seconds,
                failure_counts=failures,
            )
        )
    if not rows:
        raise ValueError(f"No valid baseline observations: {failures}")
    train, holdout = chronological_split(rows, holdout_fraction)
    with (output_dir / "observations.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=[*asdict(rows[0]), "split"])
        writer.writeheader()
        for name, bucket in (("train", train), ("holdout", holdout)):
            writer.writerows({**asdict(row), "split": name} for row in bucket)
    summary = {
        "baseline": "settlement-asian-v1",
        "asset": profile.asset,
        "vol_window_seconds": BASELINE_VOL_WINDOW_SECONDS,
        "horizons": horizons,
        "holdout_fraction": holdout_fraction,
        "max_trade_age_seconds": max_trade_age_seconds,
        "attempted_markets": len(markets),
        "rejections": failures,
        "train": summarize(train),
        "holdout": summarize(holdout),
        "note": "Market prices are recent public trades, not executable quotes. Historical publication timestamps omit network latency and revision history. No fills or profitability are inferred.",
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
    parser.add_argument("--output-dir", type=Path, default=Path("output/baseline"))
    parser.add_argument("--input-dir", type=Path)
    args = parser.parse_args()
    print(
        json.dumps(
            run_backtest(
                asset=args.asset,
                max_markets=args.max_markets,
                horizons=tuple(int(h) for h in args.horizons.split(",")),
                output_dir=args.output_dir,
                input_dir=args.input_dir,
                holdout_fraction=args.holdout_fraction,
                max_trade_age_seconds=args.max_trade_age_seconds,
            ),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
