from __future__ import annotations

from typing import Any

import numpy as np

from engine.asian_pricer import prob_levy_tw_binary
from engine.shadow.fee_model import expected_value_no_cents, expected_value_yes_cents, taker_fee_cents_per_contract


def _clip_price(price_cents: float) -> int:
    return max(1, min(99, int(round(float(price_cents)))))


def _max_drawdown_pct(equity_values: np.ndarray) -> float:
    if equity_values.size == 0:
        return 0.0
    peaks = np.maximum.accumulate(equity_values)
    drawdowns = np.where(peaks > 0.0, (peaks - equity_values) / peaks, 0.0)
    return float(np.max(drawdowns)) if drawdowns.size else 0.0


def _decision_time_seconds(rng: np.random.Generator, horizon_seconds: int) -> int:
    horizon = max(60, int(horizon_seconds))

    # Requested behavior for 15-minute contracts: choose a decision point in minute 5..13.
    if horizon >= 13 * 60:
        low = 5 * 60
        high = min(13 * 60, horizon - 60)
    else:
        low = max(20, int(round(horizon * 0.33)))
        high = max(low + 1, min(horizon - 20, int(round(horizon * 0.86))))

    if high <= low:
        return max(1, min(horizon - 1, horizon // 2))
    return int(rng.integers(low, high + 1))


def _simulate_market_yes_prob(rng: np.random.Generator, p_model: float) -> float:
    # Creates realistic market disagreement around model fair value.
    shift = float(rng.normal(loc=0.0, scale=0.06))
    return float(np.clip(p_model + shift, 0.02, 0.98))


def run_monte_carlo_replay(
    paths: np.ndarray,
    *,
    strike_usd: float,
    sigma_annual: float,
    horizon_seconds: int,
    min_edge_cents: float,
    slippage_ticks: int,
    taker_fee_curve_coeff: float,
    bankroll_start_usd: float,
    trade_size_pct: float,
    max_position_usd: float,
    levy_responsiveness: float,
    settlement_window_seconds: int = 60,
    random_seed: int | None = None,
) -> dict[str, Any]:
    if paths.ndim != 2:
        raise ValueError("paths must be a 2D array")

    n_paths = int(paths.shape[0])
    if n_paths <= 0:
        raise ValueError("paths must contain at least one path")

    rng = np.random.default_rng(random_seed)

    strike = float(strike_usd)
    sigma = max(0.01, float(sigma_annual) * float(levy_responsiveness))
    min_edge = max(0.0, float(min_edge_cents))
    slip_ticks = max(0, int(slippage_ticks))
    bankroll_cents = max(100.0, float(bankroll_start_usd) * 100.0)

    equity_curve_cents = [bankroll_cents]
    edge_samples: list[float] = []
    trade_returns: list[float] = []
    model_probs: list[float] = []
    market_probs: list[float] = []
    decision_times_sec: list[int] = []
    pnl_samples_cents: list[float] = []

    total_trades = 0
    wins = 0
    losses = 0

    n_steps = max(1, int(paths.shape[1] - 1))
    seconds_per_step = float(horizon_seconds) / float(n_steps)
    settlement_steps = max(2, int(round(float(settlement_window_seconds) / max(seconds_per_step, 1e-6))))

    for idx in range(n_paths):
        path = paths[idx]
        decision_sec = _decision_time_seconds(rng, int(horizon_seconds))
        decision_idx = int(round((float(decision_sec) / float(horizon_seconds)) * float(n_steps)))
        decision_idx = max(1, min(n_steps - 1, decision_idx))

        spot_decision = float(path[decision_idx])
        remaining_seconds = max(60.0, float(horizon_seconds) - float(decision_sec))

        pricer = prob_levy_tw_binary(
            S0=spot_decision,
            strike=strike,
            sigma_annual=sigma,
            seconds_to_expiry=remaining_seconds,
            n_fixes=max(10, int(settlement_window_seconds)),
        )
        p_model = float(pricer.p_model)
        decision_times_sec.append(int(decision_sec))

        market_yes_prob = _simulate_market_yes_prob(rng, p_model)
        spread_ticks = int(rng.integers(1, 5))
        yes_ask = _clip_price((market_yes_prob * 100.0) + (spread_ticks / 2.0))
        no_ask = _clip_price(((1.0 - market_yes_prob) * 100.0) + (spread_ticks / 2.0))

        effective_yes = _clip_price(float(yes_ask) + float(slip_ticks))
        effective_no = _clip_price(float(no_ask) + float(slip_ticks))

        edge_yes = expected_value_yes_cents(
            p_model=p_model,
            ask_price_cents=float(effective_yes),
            fee_curve_coeff=taker_fee_curve_coeff,
        )
        edge_no = expected_value_no_cents(
            p_model=p_model,
            ask_price_cents=float(effective_no),
            fee_curve_coeff=taker_fee_curve_coeff,
        )

        if edge_yes >= edge_no:
            side = "yes"
            edge = float(edge_yes)
            entry_price = int(effective_yes)
            market_prob = float(yes_ask) / 100.0
        else:
            side = "no"
            edge = float(edge_no)
            entry_price = int(effective_no)
            market_prob = float(no_ask) / 100.0

        if edge < min_edge:
            equity_curve_cents.append(bankroll_cents)
            continue

        notional_cap = min(bankroll_cents * float(trade_size_pct), float(max_position_usd) * 100.0)
        contracts = int(max(0.0, notional_cap) // max(1.0, float(entry_price)))
        if contracts <= 0:
            equity_curve_cents.append(bankroll_cents)
            continue

        settlement_tail = path[-settlement_steps:]
        settlement_avg = float(np.mean(settlement_tail))
        yes_wins = settlement_avg >= strike

        payout = 100.0 if (side == "yes") == yes_wins else 0.0
        fee_per_contract = taker_fee_cents_per_contract(float(entry_price), taker_fee_curve_coeff)
        pnl_per_contract = payout - float(entry_price) - float(fee_per_contract)
        trade_pnl = float(contracts) * pnl_per_contract

        bankroll_cents += trade_pnl
        bankroll_cents = max(0.0, bankroll_cents)

        total_trades += 1
        if trade_pnl > 0:
            wins += 1
        elif trade_pnl < 0:
            losses += 1

        edge_samples.append(edge)
        model_probs.append(p_model)
        market_probs.append(market_prob)
        pnl_samples_cents.append(trade_pnl)

        notional = float(contracts) * float(entry_price)
        if notional > 0:
            trade_returns.append(trade_pnl / notional)

        equity_curve_cents.append(bankroll_cents)

    equity_np = np.array(equity_curve_cents, dtype=float)
    pnl_np = equity_np - equity_np[0]
    pct_np = np.where(equity_np[0] > 0.0, pnl_np / equity_np[0], 0.0)

    mean_return = float(np.mean(trade_returns)) if trade_returns else 0.0
    std_return = float(np.std(trade_returns)) if trade_returns else 0.0
    sharpe = (mean_return / std_return) * (len(trade_returns) ** 0.5) if std_return > 1e-12 else 0.0

    metrics = {
        "total_markets_evaluated": int(n_paths),
        "total_trades_executed": int(total_trades),
        "trade_participation_pct": round((float(total_trades) / float(n_paths)) * 100.0, 4),
        "win_rate_pct": round((float(wins) / float(total_trades) * 100.0) if total_trades > 0 else 0.0, 4),
        "loss_rate_pct": round((float(losses) / float(total_trades) * 100.0) if total_trades > 0 else 0.0, 4),
        "average_edge_captured_cents": round(float(np.mean(edge_samples)) if edge_samples else 0.0, 6),
        "max_drawdown_pct": round(_max_drawdown_pct(equity_np) * 100.0, 4),
        "sharpe_ratio": round(float(sharpe), 6),
        "average_decision_time_min": round((float(np.mean(decision_times_sec)) / 60.0) if decision_times_sec else 0.0, 4),
        "ending_bankroll_usd": round(float(bankroll_cents) / 100.0, 6),
        "starting_bankroll_usd": round(float(equity_np[0]) / 100.0, 6),
    }

    return {
        "metrics": metrics,
        "equity_curve_profit_dollars": [float(x) / 100.0 for x in pnl_np.tolist()],
        "equity_curve_return_pct": [float(x) * 100.0 for x in pct_np.tolist()],
        "edge_samples_cents": [float(x) for x in edge_samples],
        "model_probs": [float(x) for x in model_probs],
        "market_probs": [float(x) for x in market_probs],
        "decision_time_seconds": [int(x) for x in decision_times_sec],
        "trade_pnl_cents": [float(x) for x in pnl_samples_cents],
    }
