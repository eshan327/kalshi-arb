from __future__ import annotations

import argparse
import csv
import json
from dataclasses import dataclass, asdict
from pathlib import Path

from core.config import KALSHI_ENV
from research.backtest import DEFAULT_HORIZONS_SECONDS, ModelObservation, run_backtest


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


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _market_close(row: ModelObservation) -> float:
    return float(row.eval_ts + row.seconds_to_expiry)


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
) -> tuple[list[SweepResult], dict]:
    results: list[SweepResult] = []
    combination_summaries: list[dict] = []

    for window in windows:
        for scale in scales:
            model_rows, _, _, _, _, summary = run_backtest(
                asset=asset,
                max_markets=max_markets,
                min_edge_cents=2.0,
                horizons=DEFAULT_HORIZONS_SECONDS,
                vol_window_seconds=window,
                volatility_scale=scale,
                include_tape=False,
                include_quotes=False,
                include_market_relative=False,
            )
            closes_by_market = {
                row.market_ticker: _market_close(row) for row in model_rows
            }
            ordered_markets = [
                ticker
                for ticker, _ in sorted(
                    closes_by_market.items(), key=lambda item: item[1]
                )
            ]
            midpoint = len(ordered_markets) // 2
            development = set(ordered_markets[:midpoint])
            holdout = set(ordered_markets[midpoint:])
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
            combination_summaries.append(
                {
                    "vol_window_seconds": window,
                    "volatility_scale": scale,
                    "markets": len(ordered_markets),
                    "calibration_rejections": summary.get(
                        "calibration_rejections", {}
                    ),
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
        row
        for row in combination_summaries
        if row["development_brier"] is not None
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
        "max_markets": max_markets,
        "selection_rule": (
            "Choose the window/scale with lowest development-half Brier score; "
            "evaluate that frozen choice on the newer holdout half."
        ),
        "selected_on_development": selected,
        "production_baseline": production,
        "combinations": combination_summaries,
    }
    return results, summary


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
    results, summary = run_sweep(
        asset=args.asset,
        max_markets=max(2, args.max_markets),
        windows=windows,
        scales=scales,
    )

    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    rows = [asdict(row) for row in results]
    with (out / "sweep.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]) if rows else [])
        if rows:
            writer.writeheader()
            writer.writerows(rows)
    (out / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
