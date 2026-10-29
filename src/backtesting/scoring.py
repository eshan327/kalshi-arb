"""Score baseline observations with a chronological holdout."""

from __future__ import annotations

import math

from backtesting.model import ModelObservation


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
    """Keep training labels available before the first holdout observation."""
    if not 0 < holdout_fraction < 1:
        raise ValueError("Holdout fraction must be between zero and one")
    closes = sorted({row.close_ts for row in rows})
    if len(closes) < 2:
        raise ValueError("At least two market close times are required for a holdout")
    boundary = closes[
        max(1, min(len(closes) - 1, int(len(closes) * (1 - holdout_fraction))))
    ]
    ordered = sorted(rows, key=lambda row: (row.eval_ts, row.market_ticker))
    holdout = [row for row in ordered if row.close_ts >= boundary]
    first_holdout_decision = min(row.eval_ts for row in holdout)
    train = [row for row in ordered if row.close_ts < boundary and row.outcome_ts < first_holdout_decision]
    crossing = [row for row in ordered if row.close_ts < boundary and row.outcome_ts >= first_holdout_decision]
    return train, holdout, crossing


def summarize(rows: list[ModelObservation]) -> dict:
    def scores(bucket):
        paired = [row for row in bucket if row.p_market is not None]
        quoted = [row for row in bucket if row.yes_bid_cents is not None and row.yes_ask_cents is not None]
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
            "paired_quote_baseline": score_predictions(
                [r.p_model for r in quoted], [r.actual_yes for r in quoted]
            ),
            "paired_quote_midpoint": score_predictions(
                [(r.yes_bid_cents + r.yes_ask_cents) / 200 for r in quoted],
                [r.actual_yes for r in quoted],
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
