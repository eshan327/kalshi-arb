from __future__ import annotations

import argparse
import csv
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from core.config import KALSHI_ENV
from core.market_metadata import extract_suggested_strike
from core.market_profiles import get_market_profile
from data.kalshi_rest import get_settled_markets
from engine.market_stream.discovery import parse_iso8601_to_epoch
from research.backtest import (
    CF_HISTORY_DATA_LAG_BUFFER_SEC,
    DEFAULT_HORIZONS_SECONDS,
    ModelObservation,
    calibrate_market,
    fetch_cf_feeds,
)


@dataclass(frozen=True)
class SweepResult:
    vol_window_seconds: float
    volatility_scale: float
    split: str
    horizon_seconds: int | None
    observations: int
    markets: int
    mean_brier: float | None
    mean_log_loss: float | None


@dataclass(frozen=True)
class SweepObservation:
    vol_window_seconds: float
    volatility_scale: float
    split: str
    market_ticker: str
    eval_ts: float
    horizon_seconds: int
    p_model: float
    actual_yes: int
    brier: float
    log_loss: float


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _summarize_rows(
    rows: list[ModelObservation],
    *,
    window: float,
    scale: float,
    split: str,
) -> list[SweepResult]:
    out = [
        SweepResult(
            vol_window_seconds=window,
            volatility_scale=scale,
            split=split,
            horizon_seconds=None,
            observations=len(rows),
            markets=len({row.market_ticker for row in rows}),
            mean_brier=_mean([row.brier for row in rows]),
            mean_log_loss=_mean([row.log_loss for row in rows]),
        )
    ]
    for horizon in sorted(
        {row.nominal_horizon_seconds for row in rows}, reverse=True
    ):
        bucket = [row for row in rows if row.nominal_horizon_seconds == horizon]
        out.append(
            SweepResult(
                vol_window_seconds=window,
                volatility_scale=scale,
                split=split,
                horizon_seconds=horizon,
                observations=len(bucket),
                markets=len({row.market_ticker for row in bucket}),
                mean_brier=_mean([row.brier for row in bucket]),
                mean_log_loss=_mean([row.log_loss for row in bucket]),
            )
        )
    return out


def run_sweep(
    *,
    asset: str,
    max_markets: int,
    windows: tuple[float, ...],
    scales: tuple[float, ...],
) -> tuple[list[SweepResult], list[SweepObservation], dict]:
    profile = get_market_profile(asset)
    cutoff = time.time() - CF_HISTORY_DATA_LAG_BUFFER_SEC
    markets = [
        market
        for market in get_settled_markets(profile.kalshi_series_ticker)
        if str(market.get("result") or "").lower() in {"yes", "no"}
        and extract_suggested_strike(market) is not None
        and (parse_iso8601_to_epoch(market.get("close_time")) or float("inf"))
        <= cutoff
    ]
    markets.sort(key=lambda row: parse_iso8601_to_epoch(row.get("close_time")) or 0)
    if max_markets > 0:
        markets = markets[-max_markets:]
    if len(markets) < 2:
        raise ValueError("Need at least two settled markets for a chronological split")

    closes = [
        parse_iso8601_to_epoch(market.get("close_time"))
        for market in markets
    ]
    closes = [ts for ts in closes if ts is not None]
    max_window = max(windows) if windows else 300.0
    max_horizon = max(DEFAULT_HORIZONS_SECONDS)
    spot_ticks, fix_ticks, resolution = fetch_cf_feeds(
        profile,
        min(closes) - max(20 * 60, max_horizon + max_window),
        max(closes) + 1,
    )
    subsecond_offset = 0.4 if resolution == "PER_200MS" else 0.0

    ordered_tickers = [str(market["ticker"]) for market in markets]
    midpoint = len(ordered_tickers) // 2
    development = set(ordered_tickers[:midpoint])
    holdout = set(ordered_tickers[midpoint:])

    results: list[SweepResult] = []
    observations: list[SweepObservation] = []
    combination_summaries: list[dict] = []

    for window in windows:
        for scale in scales:
            model_rows: list[ModelObservation] = []
            failures: dict[str, int] = {}
            for market in markets:
                model_rows.extend(
                    calibrate_market(
                        market,
                        asset=profile.asset,
                        spot_ticks=spot_ticks,
                        fix_ticks=fix_ticks,
                        horizons=DEFAULT_HORIZONS_SECONDS,
                        subsecond_offset=subsecond_offset,
                        vol_window_seconds=window,
                        volatility_scale=scale,
                        failure_counts=failures,
                    )
                )

            split_rows = {
                "all": model_rows,
                "development": [
                    row for row in model_rows if row.market_ticker in development
                ],
                "holdout": [
                    row for row in model_rows if row.market_ticker in holdout
                ],
            }
            for split, rows in split_rows.items():
                results.extend(
                    _summarize_rows(
                        rows,
                        window=window,
                        scale=scale,
                        split=split,
                    )
                )
                if split in {"development", "holdout"}:
                    observations.extend(
                        SweepObservation(
                            vol_window_seconds=window,
                            volatility_scale=scale,
                            split=split,
                            market_ticker=row.market_ticker,
                            eval_ts=row.eval_ts,
                            horizon_seconds=row.nominal_horizon_seconds,
                            p_model=row.p_model,
                            actual_yes=row.actual_yes,
                            brier=row.brier,
                            log_loss=row.log_loss,
                        )
                        for row in rows
                    )

            combination_summaries.append(
                {
                    "vol_window_seconds": window,
                    "volatility_scale": scale,
                    "markets": len(ordered_tickers),
                    "calibration_rejections": dict(sorted(failures.items())),
                    "development_brier": _mean(
                        [row.brier for row in split_rows["development"]]
                    ),
                    "development_log_loss": _mean(
                        [row.log_loss for row in split_rows["development"]]
                    ),
                    "holdout_brier": _mean(
                        [row.brier for row in split_rows["holdout"]]
                    ),
                    "holdout_log_loss": _mean(
                        [row.log_loss for row in split_rows["holdout"]]
                    ),
                }
            )

    selectable = [
        row for row in combination_summaries if row["development_brier"] is not None
    ]
    selected = (
        min(
            selectable,
            key=lambda row: (
                row["development_brier"],
                row["development_log_loss"],
            ),
        )
        if selectable
        else None
    )
    production = next(
        (
            row
            for row in combination_summaries
            if row["vol_window_seconds"] == 300.0
            and row["volatility_scale"] == 1.0
        ),
        None,
    )
    summary = {
        "asset": asset,
        "markets": len(ordered_tickers),
        "development_markets": len(development),
        "holdout_markets": len(holdout),
        "cf_spot_resolution": resolution,
        "selection_rule": (
            "Choose the window/scale with lowest development-half Brier score; "
            "evaluate that frozen choice on the newer holdout half."
        ),
        "selected_on_development": selected,
        "production_baseline": production,
        "combinations": combination_summaries,
    }
    return results, observations, summary


def _write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    if KALSHI_ENV != "prod":
        raise RuntimeError(
            "Historical research uses production Kalshi/CF data; set KALSHI_ENV=prod."
        )
    parser = argparse.ArgumentParser(
        description="Chronological volatility-window/scale sweep for the Asian model."
    )
    parser.add_argument("asset", nargs="?", default="BTC")
    parser.add_argument("--max-markets", type=int, default=200)
    parser.add_argument("--windows", default="60,120,300,600")
    parser.add_argument("--scales", default="0.75,1.0,1.25")
    parser.add_argument("--output-dir", default="output/model_sweep")
    args = parser.parse_args()

    windows = tuple(
        float(value) for value in args.windows.split(",") if value.strip()
    )
    scales = tuple(
        float(value) for value in args.scales.split(",") if value.strip()
    )
    results, observations, summary = run_sweep(
        asset=args.asset,
        max_markets=max(2, args.max_markets),
        windows=windows,
        scales=scales,
    )

    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    _write_csv(out / "sweep.csv", [asdict(row) for row in results])
    _write_csv(
        out / "sweep_observations.csv",
        [asdict(row) for row in observations],
    )
    (out / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
