"""Run one external-signal study across 15-minute markets and Perps."""

from __future__ import annotations

import argparse
import csv
import json
import re
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from perps_backtest import run_backtest as replay_perps
from taker_backtest import run_backtest as replay_15m


def _totals(rows: list[dict]) -> dict:
    scored = [row for row in rows if row["net_pnl_dollars"] != ""]
    return {
        "signals": len(rows),
        "scored": len(scored),
        "open_positions": sum(row["open_contracts"] != "0" for row in rows),
        "unscored": len(rows) - len(scored),
        "scored_net_pnl_dollars": str(sum(
            (Decimal(row["net_pnl_dollars"]) for row in scored), Decimal(0)
        )),
    }


def run_study(config_path: Path, output_dir: Path) -> dict:
    config = json.loads(config_path.read_text())
    try:
        boundary = datetime.fromisoformat(config["holdout_start"].replace("Z", "+00:00"))
        if boundary.tzinfo is None:
            raise ValueError
        holdout_ts = boundary.timestamp()
        legs = config["legs"]
        if not isinstance(legs, list) or not legs:
            raise ValueError
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Config needs holdout_start with a UTC offset and nonempty legs") from exc
    names = set()
    for leg in legs:
        if not isinstance(leg, dict):
            raise ValueError("Each leg must be an object")
        name = leg.get("name")
        if (not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", name)
            or name in names or leg.get("product") not in {"15m", "perps"}):
            raise ValueError("Each leg needs a unique simple name and product 15m or perps")
        names.add(name)
        for key in ("history_dir", "capture_path", "signals_path"):
            if not isinstance(leg.get(key), str) or not leg[key]:
                raise ValueError(f"{name} needs {key}")
        fee_key = "fee_multiplier" if leg["product"] == "15m" else "taker_fee_rate"
        if fee_key not in leg:
            raise ValueError(f"{name} needs {fee_key}")
        for key, default in (("latency_ms", 100), ("max_book_age_ms", 2000)):
            value = leg.get(key, config.get(key, default))
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} needs a nonnegative integer {key}")

    def path(leg: dict, key: str) -> Path:
        return config_path.parent / leg[key]

    def setting(leg: dict, key: str, default):
        return leg.get(key, config.get(key, default))

    output_dir.mkdir(parents=True, exist_ok=False)
    combined = []
    for leg in legs:
        name, product = leg["name"], leg["product"]
        destination = output_dir / name
        common = dict(
            capture_path=path(leg, "capture_path"),
            signals_path=path(leg, "signals_path"), output_dir=destination,
            latency_ms=setting(leg, "latency_ms", 100),
            max_book_age_ms=setting(leg, "max_book_age_ms", 2000),
            depth_fraction=Decimal(str(setting(leg, "depth_fraction", "0.5"))),
        )
        if product == "15m":
            replay_15m(
                input_dir=path(leg, "history_dir"), **common,
                fee_multiplier=Decimal(str(leg["fee_multiplier"])),
                balance_precision=Decimal(str(leg.get("balance_precision", "0.0001"))),
                holdout_start=holdout_ts,
            )
            filename, clock, decision, ticker, pnl = (
                "fills.csv", "outcome_ts", "decision_ts", "market_ticker", "pnl_dollars"
            )
        else:
            replay_perps(
                history_dir=path(leg, "history_dir"), **common,
                taker_fee_rate=Decimal(str(leg["taker_fee_rate"])),
            )
            filename, clock, decision, ticker, pnl = (
                "round_trips.csv", "exit_arrival_ts", "entry_ts", "ticker", "net_pnl_dollars"
            )
        with (destination / filename).open(newline="") as handle:
            for result in csv.DictReader(handle):
                status = result["status"]
                value = result[pnl]
                if product == "perps" and status in {
                    "no_continuous_book", "stale_book", "insufficient_depth"
                }:
                    value = "0"
                decision_ts, outcome_ts = float(result[decision]), float(result[clock])
                split = ("train" if outcome_ts < holdout_ts else
                         "holdout" if decision_ts >= holdout_ts else "crossing")
                combined.append({
                    "leg": name, "product": product, "ticker": result[ticker],
                    "decision_ts": result[decision], "outcome_ts": result[clock],
                    "split": split,
                    "status": status, "open_contracts": result.get("open_contracts", "0"),
                    "net_pnl_dollars": value,
                })
    combined.sort(key=lambda row: (float(row["outcome_ts"]), row["leg"]))
    with (output_dir / "results.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(combined[0]))
        writer.writeheader()
        writer.writerows(combined)
    summary = {
        "holdout_start": boundary.isoformat(),
        "all": _totals(combined),
        "train": _totals([row for row in combined if row["split"] == "train"]),
        "holdout": _totals([row for row in combined if row["split"] == "holdout"]),
        "crossing": _totals([row for row in combined if row["split"] == "crossing"]),
        "note": "PnL sums scored, settled predictions and closed Perps trades; no shared capital or liquidation model.",
    }
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run_study(args.config, args.output_dir), indent=2))


if __name__ == "__main__":
    main()
