from __future__ import annotations

import argparse
import csv
import json
import math
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.market_metadata import extract_settlement_decimals, extract_suggested_strike
from core.market_profiles import get_market_profile
from data.kalshi_rest import (
    get_cfbenchmarks_history,
    get_historical_candlesticks,
    get_historical_markets,
)
from engine.market_stream.discovery import parse_iso8601_to_epoch
from engine.pricing.pipeline import compute_pricing_snapshot
from engine.trading.fees import taker_fee_cents_per_contract


@dataclass(frozen=True)
class BacktestObservation:
    market_ticker: str
    eval_ts: float
    seconds_to_expiry: float
    spot: float
    strike: float
    p_model: float
    actual_yes: int
    yes_bid_cents: float | None
    yes_ask_cents: float | None
    no_ask_cents: float | None
    yes_edge_cents: float | None
    no_edge_cents: float | None
    sigma_annual: float
    regime: str
    brier: float
    log_loss: float


@dataclass(frozen=True)
class BacktestTrade:
    market_ticker: str
    eval_ts: float
    side: str
    model_probability: float
    entry_cents: float
    fee_cents: float
    edge_cents: float
    pnl_cents: float
    won: bool


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
    """Normalize CF history payloads to sorted, deduplicated one-second values."""
    by_second: dict[int, float] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        ts = None
        for key in ("time", "timestamp", "source_ts_ms", "ts"):
            if key in row:
                ts = _parse_timestamp(row.get(key))
                if ts is not None:
                    break
        price = None
        for key in ("value", "value_usd", "price"):
            if key not in row:
                continue
            try:
                candidate = float(row[key])
            except (TypeError, ValueError):
                continue
            if math.isfinite(candidate) and candidate > 0:
                price = candidate
                break
        if ts is None or price is None:
            continue

        # The settlement model uses the one-second CF fixing grid. Historical
        # endpoints can contain sub-second data, so collapse deterministically
        # to the final observation at each integer second.
        second = int(math.floor(ts))
        by_second[second] = price

    return [
        {"ts": float(ts), "price": by_second[ts]}
        for ts in sorted(by_second)
    ]


def _hour_start(ts: float) -> float:
    return float(int(ts) // 3600 * 3600)


def _iso_hour(ts: float) -> str:
    return datetime.fromtimestamp(_hour_start(ts), UTC).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def fetch_cf_range(index_id: str, start_ts: float, end_ts: float) -> list[dict[str, float]]:
    """Fetch and normalize all CF one-second history needed for a replay range."""
    raw: list[dict] = []
    cursor = _hour_start(start_ts)
    final_hour = _hour_start(end_ts)
    while cursor <= final_hour:
        raw.extend(
            get_cfbenchmarks_history(
                index_id,
                timestamp=_iso_hour(cursor),
                timespan="HOUR",
                max_resolution="PER_SECOND",
            )
        )
        cursor += 3600

    return [
        tick
        for tick in normalize_cf_history(raw)
        if start_ts <= tick["ts"] <= end_ts
    ]


def _latest_tick(ticks: list[dict[str, float]], now_ts: float) -> dict[str, float] | None:
    latest = None
    for tick in ticks:
        if tick["ts"] > now_ts:
            break
        latest = tick
    return latest


def _settlement_state(
    ticks: list[dict[str, float]], *, now_ts: float, close_ts: float, window: int
) -> dict[str, Any]:
    latest = _latest_tick(ticks, now_ts)
    if latest is None:
        return {"connected": False, "timestamp": 0.0}

    state: dict[str, Any] = {
        "connected": True,
        "timestamp": latest["ts"],
    }
    start = close_ts - window
    if now_ts <= start:
        return state

    fixes = [
        tick
        for tick in ticks
        if start < tick["ts"] <= min(now_ts, close_ts)
    ]
    if not fixes:
        return state

    mean = sum(tick["price"] for tick in fixes) / len(fixes)
    state.update(
        average_ts=latest["ts"],
        final_average={
            "start": start,
            "end": fixes[-1]["ts"],
            "count": len(fixes),
            "value": mean,
        },
    )
    return state


def _dollars_to_cents(value: Any) -> float | None:
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x * 100.0 if math.isfinite(x) else None


def _candle_quote(candle: dict) -> tuple[float | None, float | None, float | None]:
    yes_bid = _dollars_to_cents((candle.get("yes_bid") or {}).get("close"))
    yes_ask = _dollars_to_cents((candle.get("yes_ask") or {}).get("close"))
    no_ask = None if yes_bid is None else 100.0 - yes_bid
    return yes_bid, yes_ask, no_ask


def _scores(p: float, y: int) -> tuple[float, float]:
    p = min(1.0 - 1e-12, max(1e-12, float(p)))
    return (p - y) ** 2, -(y * math.log(p) + (1 - y) * math.log(1 - p))


def backtest_market(
    market: dict,
    *,
    asset: str,
    cf_ticks: list[dict[str, float]],
    min_edge_cents: float,
) -> tuple[list[BacktestObservation], BacktestTrade | None]:
    profile = get_market_profile(asset)
    ticker = str(market.get("ticker") or "")
    strike = extract_suggested_strike(market)
    close_ts = parse_iso8601_to_epoch(market.get("close_time"))
    open_ts = parse_iso8601_to_epoch(market.get("open_time"))
    result = str(market.get("result") or "").lower()
    if not ticker or strike is None or close_ts is None or result not in {"yes", "no"}:
        return [], None

    start_ts = max(open_ts or close_ts - 900, close_ts - 900)
    candles = get_historical_candlesticks(
        ticker=ticker,
        start_ts=int(start_ts),
        end_ts=int(close_ts),
        period_interval=1,
    )
    decimals = extract_settlement_decimals(market, profile.settlement_decimals_fallback)
    actual_yes = 1 if result == "yes" else 0

    observations: list[BacktestObservation] = []
    trade: BacktestTrade | None = None

    for candle in sorted(candles, key=lambda row: int(row.get("end_period_ts") or 0)):
        eval_ts = float(candle.get("end_period_ts") or 0)
        if eval_ts <= 0 or eval_ts >= close_ts:
            continue

        latest = _latest_tick(cf_ticks, eval_ts)
        if latest is None:
            continue
        state = _settlement_state(
            cf_ticks,
            now_ts=eval_ts,
            close_ts=close_ts,
            window=profile.settlement_window_seconds,
        )
        available_ticks = [tick for tick in cf_ticks if tick["ts"] <= eval_ts]
        snapshot = compute_pricing_snapshot(
            profile=profile,
            feed_asset=asset,
            spot=latest["price"],
            ticks=available_ticks,
            strike=strike,
            market_ticker=ticker,
            close_time_iso=market.get("close_time"),
            settlement_decimals=decimals,
            index_state=state,
            now_ts=eval_ts,
        )
        if not snapshot.get("ready") or snapshot.get("vol_is_fallback"):
            continue

        p = float(snapshot["p_model"])
        yes_bid, yes_ask, no_ask = _candle_quote(candle)
        yes_edge = None
        no_edge = None
        if yes_ask is not None:
            yes_edge = p * 100.0 - yes_ask - taker_fee_cents_per_contract(yes_ask)
        if no_ask is not None:
            no_edge = (1.0 - p) * 100.0 - no_ask - taker_fee_cents_per_contract(no_ask)
        brier, log_loss = _scores(p, actual_yes)

        observations.append(
            BacktestObservation(
                market_ticker=ticker,
                eval_ts=eval_ts,
                seconds_to_expiry=close_ts - eval_ts,
                spot=float(latest["price"]),
                strike=float(strike),
                p_model=p,
                actual_yes=actual_yes,
                yes_bid_cents=yes_bid,
                yes_ask_cents=yes_ask,
                no_ask_cents=no_ask,
                yes_edge_cents=yes_edge,
                no_edge_cents=no_edge,
                sigma_annual=float(snapshot["sigma_annual"]),
                regime=str(snapshot["regime"]),
                brier=brier,
                log_loss=log_loss,
            )
        )

        # Coarse execution approximation: one contract, first 1-minute candle-close
        # signal that clears fees and the configured edge, held through settlement.
        if trade is None:
            candidates = []
            if yes_edge is not None and yes_ask is not None:
                candidates.append(("yes", p, yes_ask, yes_edge))
            if no_edge is not None and no_ask is not None:
                candidates.append(("no", 1.0 - p, no_ask, no_edge))
            if candidates:
                side, model_p, entry, edge = max(candidates, key=lambda row: row[3])
                if edge >= min_edge_cents:
                    fee = taker_fee_cents_per_contract(entry)
                    won = (side == "yes" and actual_yes == 1) or (
                        side == "no" and actual_yes == 0
                    )
                    payoff = 100.0 if won else 0.0
                    trade = BacktestTrade(
                        market_ticker=ticker,
                        eval_ts=eval_ts,
                        side=side,
                        model_probability=model_p,
                        entry_cents=entry,
                        fee_cents=fee,
                        edge_cents=edge,
                        pnl_cents=payoff - entry - fee,
                        won=won,
                    )

    return observations, trade


def summarize(
    observations: list[BacktestObservation], trades: list[BacktestTrade]
) -> dict[str, Any]:
    def mean(values: list[float]) -> float | None:
        return sum(values) / len(values) if values else None

    calibration = []
    for lower in [i / 10 for i in range(10)]:
        upper = lower + 0.1
        bucket = [
            row
            for row in observations
            if lower <= row.p_model < upper or (upper == 1.0 and row.p_model == 1.0)
        ]
        if bucket:
            calibration.append(
                {
                    "range": f"{lower:.1f}-{upper:.1f}",
                    "n": len(bucket),
                    "mean_model": mean([row.p_model for row in bucket]),
                    "actual_yes_rate": mean([float(row.actual_yes) for row in bucket]),
                }
            )

    pnl = sum(row.pnl_cents for row in trades)
    cost = sum(row.entry_cents + row.fee_cents for row in trades)
    return {
        "observations": len(observations),
        "markets_observed": len({row.market_ticker for row in observations}),
        "mean_brier": mean([row.brier for row in observations]),
        "mean_log_loss": mean([row.log_loss for row in observations]),
        "trades": len(trades),
        "wins": sum(1 for row in trades if row.won),
        "win_rate": mean([1.0 if row.won else 0.0 for row in trades]),
        "pnl_cents": pnl,
        "roi_on_entry_cost": (pnl / cost) if cost > 0 else None,
        "mean_signal_edge_cents": mean([row.edge_cents for row in trades]),
        "calibration": calibration,
        "execution_note": (
            "Trade PnL uses 1-minute historical quote closes and one contract held to "
            "settlement. It is an alpha screen, not an L2/latency-accurate execution replay."
        ),
    }


def run_backtest(
    *,
    asset: str,
    max_markets: int,
    min_edge_cents: float,
) -> tuple[list[BacktestObservation], list[BacktestTrade], dict[str, Any]]:
    profile = get_market_profile(asset)
    markets = [
        market
        for market in get_historical_markets(series_ticker=profile.kalshi_series_ticker)
        if str(market.get("result") or "").lower() in {"yes", "no"}
    ]
    markets.sort(
        key=lambda row: parse_iso8601_to_epoch(row.get("close_time")) or 0,
        reverse=True,
    )
    if max_markets > 0:
        markets = markets[:max_markets]
    if not markets:
        return [], [], summarize([], [])

    closes = [
        parse_iso8601_to_epoch(market.get("close_time"))
        for market in markets
    ]
    closes = [ts for ts in closes if ts is not None]
    cf_ticks = fetch_cf_range(
        profile.index_id,
        min(closes) - 20 * 60,
        max(closes) + 1,
    )

    observations: list[BacktestObservation] = []
    trades: list[BacktestTrade] = []
    for market in markets:
        market_rows, trade = backtest_market(
            market,
            asset=profile.asset,
            cf_ticks=cf_ticks,
            min_edge_cents=min_edge_cents,
        )
        observations.extend(market_rows)
        if trade is not None:
            trades.append(trade)

    return observations, trades, summarize(observations, trades)


def _write_csv(path: Path, rows: list[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    records = [asdict(row) for row in rows]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(records[0]))
        writer.writeheader()
        writer.writerows(records)


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest the Kalshi 15-minute crypto model.")
    parser.add_argument("asset", nargs="?", default="BTC")
    parser.add_argument("--max-markets", type=int, default=100)
    parser.add_argument("--min-edge-cents", type=float, default=2.0)
    parser.add_argument("--output-dir", default="output/backtests")
    args = parser.parse_args()

    observations, trades, summary = run_backtest(
        asset=args.asset,
        max_markets=max(0, args.max_markets),
        min_edge_cents=max(0.0, args.min_edge_cents),
    )
    out = Path(args.output_dir)
    _write_csv(out / "observations.csv", observations)
    _write_csv(out / "trades.csv", trades)
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
