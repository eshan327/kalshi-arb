from __future__ import annotations

from typing import Any

import numpy as np

from engine.asian_pricer import prob_levy_tw_binary
from engine.shadow.fee_model import (
    expected_value_no_cents,
    expected_value_yes_cents,
    quarter_kelly_fraction_binary,
    taker_fee_cents_per_contract,
)

PROBABILITY_LOWER_BOUND = 0.20
PROBABILITY_UPPER_BOUND = 0.80
MAX_POSITION_NOTIONAL_CENTS = 5_000.0
MAX_CLIP_CONTRACTS = 50
EVAL_INTERVAL_SECONDS = 5
MARKET_INEFFICIENCY_BIAS_CENTS = 1.60


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
    shift = float(rng.normal(loc=0.0, scale=0.012))
    return float(np.clip(p_model + shift, 0.02, 0.98))


def _kelly_target_contracts(*, p_win: float, cost_cents: int, bankroll_cents: float) -> int:
    px = max(1, int(cost_cents))
    kelly_fraction = quarter_kelly_fraction_binary(p_win=float(p_win), cost_cents=float(px))
    notional_target = min(float(MAX_POSITION_NOTIONAL_CENTS), float(bankroll_cents) * float(kelly_fraction))
    return max(0, int(notional_target // float(px)))


def _generate_quotes(
    rng: np.random.Generator,
    *,
    p_model: float,
) -> tuple[int, int, int, int]:
    directional_bias = -MARKET_INEFFICIENCY_BIAS_CENTS if float(p_model) >= 0.5 else MARKET_INEFFICIENCY_BIAS_CENTS
    mid_yes = _clip_price(
        (float(p_model) * 100.0)
        + float(directional_bias)
        + float(rng.normal(loc=0.0, scale=0.75))
    )
    spread = int(rng.integers(2, 6))
    half = max(1.0, float(spread) / 2.0)

    yes_bid = _clip_price(float(mid_yes) - half)
    yes_ask = _clip_price(float(mid_yes) + half)
    if yes_bid >= yes_ask:
        yes_ask = min(99, yes_bid + 1)

    no_ask = _clip_price(100.0 - float(yes_bid))
    no_bid = _clip_price(100.0 - float(yes_ask))
    if no_bid >= no_ask:
        no_ask = min(99, no_bid + 1)

    return yes_bid, yes_ask, no_bid, no_ask


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
    _ = float(trade_size_pct)  # kept for API compatibility; sizing now Kelly-based.
    _ = float(max_position_usd)  # hard-capped to $50 for retail risk control.

    min_edge = max(0.0, float(min_edge_cents))
    slip_ticks = max(0, int(slippage_ticks))
    cash_cents = max(100.0, float(bankroll_start_usd) * 100.0)

    equity_curve_cents = [cash_cents]
    edge_samples: list[float] = []
    trade_returns: list[float] = []
    model_probs: list[float] = []
    market_probs: list[float] = []
    decision_times_sec: list[int] = []
    pnl_samples_cents: list[float] = []

    total_trades = 0
    entry_trades = 0
    exit_trades = 0
    wins = 0
    losses = 0
    markets_with_activity = 0

    n_steps = max(1, int(paths.shape[1] - 1))
    seconds_per_step = float(horizon_seconds) / float(n_steps)
    settlement_steps = max(2, int(round(float(settlement_window_seconds) / max(seconds_per_step, 1e-6))))
    eval_step = max(1, int(round(float(EVAL_INTERVAL_SECONDS) / max(seconds_per_step, 1e-6))))

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

        yes_qty = 0
        no_qty = 0
        yes_avg = 0.0
        no_avg = 0.0
        market_active = False

        for step in range(int(decision_idx), int(n_steps), int(eval_step)):
            remaining_seconds_step = max(30.0, float(horizon_seconds) - (float(step) * seconds_per_step))
            spot_step = float(path[step])

            step_pricer = prob_levy_tw_binary(
                S0=spot_step,
                strike=strike,
                sigma_annual=sigma,
                seconds_to_expiry=remaining_seconds_step,
                n_fixes=max(10, int(settlement_window_seconds)),
            )
            p_step = float(step_pricer.p_model)

            market_yes_prob = _simulate_market_yes_prob(rng, p_step)
            yes_bid, yes_ask, no_bid, no_ask = _generate_quotes(rng, p_model=market_yes_prob)

            buy_yes = _clip_price(float(yes_ask) + float(slip_ticks))
            buy_no = _clip_price(float(no_ask) + float(slip_ticks))
            sell_yes = _clip_price(float(yes_bid) - float(slip_ticks))
            sell_no = _clip_price(float(no_bid) - float(slip_ticks))

            fair_yes = float(p_step) * 100.0
            fair_no = (1.0 - float(p_step)) * 100.0

            # Dynamic scale-out when edge flips against held inventory.
            exit_buffer = max(float(min_edge), 1.0)

            if yes_qty > 0 and float(yes_bid) > (fair_yes + float(exit_buffer)):
                qty = min(int(yes_qty), int(MAX_CLIP_CONTRACTS))
                fee_per = taker_fee_cents_per_contract(float(sell_yes), taker_fee_curve_coeff)
                fees = fee_per * float(qty)
                proceeds = float(qty) * float(sell_yes) - fees
                pnl = float(qty) * (float(sell_yes) - float(yes_avg)) - fees

                cash_cents += proceeds
                yes_qty -= qty
                if yes_qty <= 0:
                    yes_qty = 0
                    yes_avg = 0.0

                total_trades += 1
                exit_trades += 1
                market_active = True
                pnl_samples_cents.append(float(pnl))
                if pnl > 0:
                    wins += 1
                elif pnl < 0:
                    losses += 1

                notional = float(qty) * float(max(1, int(sell_yes)))
                if notional > 0:
                    trade_returns.append(float(pnl) / notional)

            if no_qty > 0 and float(no_bid) > (fair_no + float(exit_buffer)):
                qty = min(int(no_qty), int(MAX_CLIP_CONTRACTS))
                fee_per = taker_fee_cents_per_contract(float(sell_no), taker_fee_curve_coeff)
                fees = fee_per * float(qty)
                proceeds = float(qty) * float(sell_no) - fees
                pnl = float(qty) * (float(sell_no) - float(no_avg)) - fees

                cash_cents += proceeds
                no_qty -= qty
                if no_qty <= 0:
                    no_qty = 0
                    no_avg = 0.0

                total_trades += 1
                exit_trades += 1
                market_active = True
                pnl_samples_cents.append(float(pnl))
                if pnl > 0:
                    wins += 1
                elif pnl < 0:
                    losses += 1

                notional = float(qty) * float(max(1, int(sell_no)))
                if notional > 0:
                    trade_returns.append(float(pnl) / notional)

            # Entry guardrails and Kelly sizing only inside fat part of model distribution.
            if not (PROBABILITY_LOWER_BOUND <= float(p_step) <= PROBABILITY_UPPER_BOUND):
                continue

            edge_yes = expected_value_yes_cents(
                p_model=p_step,
                ask_price_cents=float(buy_yes),
                fee_curve_coeff=taker_fee_curve_coeff,
            )
            edge_no = expected_value_no_cents(
                p_model=p_step,
                ask_price_cents=float(buy_no),
                fee_curve_coeff=taker_fee_curve_coeff,
            )

            if edge_yes >= edge_no:
                side = "yes"
                edge = float(edge_yes)
                p_win = float(p_step)
                ask_price = int(buy_yes)
                current_qty = int(yes_qty)
                market_prob = float(yes_ask) / 100.0
            else:
                side = "no"
                edge = float(edge_no)
                p_win = 1.0 - float(p_step)
                ask_price = int(buy_no)
                current_qty = int(no_qty)
                market_prob = float(no_ask) / 100.0

            if edge < min_edge:
                continue

            mtm_value = float(yes_qty) * float(yes_bid) + float(no_qty) * float(no_bid)
            equity_cents_step = float(cash_cents) + mtm_value
            target_qty = _kelly_target_contracts(
                p_win=float(p_win),
                cost_cents=int(ask_price),
                bankroll_cents=float(max(100.0, equity_cents_step)),
            )
            add_qty = max(0, int(target_qty) - int(current_qty))
            qty = min(int(add_qty), int(MAX_CLIP_CONTRACTS))
            if qty <= 0:
                continue

            fee_per = taker_fee_cents_per_contract(float(ask_price), taker_fee_curve_coeff)
            fees = fee_per * float(qty)
            cost = float(qty) * float(ask_price) + fees
            if cost > float(cash_cents):
                continue

            cash_cents -= cost
            if side == "yes":
                weighted = (float(yes_avg) * float(yes_qty)) + (float(ask_price) * float(qty))
                yes_qty += qty
                yes_avg = weighted / max(1.0, float(yes_qty))
            else:
                weighted = (float(no_avg) * float(no_qty)) + (float(ask_price) * float(qty))
                no_qty += qty
                no_avg = weighted / max(1.0, float(no_qty))

            total_trades += 1
            entry_trades += 1
            market_active = True
            edge_samples.append(float(edge))
            model_probs.append(float(p_step))
            market_probs.append(float(market_prob))

        # Force-close remaining inventory at settlement.
        settlement_tail = path[-settlement_steps:]
        settlement_avg = float(np.mean(settlement_tail))
        yes_wins = settlement_avg >= strike

        if yes_qty > 0:
            payout = 100.0 if yes_wins else 0.0
            pnl = float(yes_qty) * (payout - float(yes_avg))
            cash_cents += float(yes_qty) * payout
            total_trades += 1
            exit_trades += 1
            market_active = True
            pnl_samples_cents.append(float(pnl))
            if pnl > 0:
                wins += 1
            elif pnl < 0:
                losses += 1
            notional = float(yes_qty) * float(max(1.0, float(yes_avg)))
            if notional > 0:
                trade_returns.append(float(pnl) / notional)

        if no_qty > 0:
            payout = 0.0 if yes_wins else 100.0
            pnl = float(no_qty) * (payout - float(no_avg))
            cash_cents += float(no_qty) * payout
            total_trades += 1
            exit_trades += 1
            market_active = True
            pnl_samples_cents.append(float(pnl))
            if pnl > 0:
                wins += 1
            elif pnl < 0:
                losses += 1
            notional = float(no_qty) * float(max(1.0, float(no_avg)))
            if notional > 0:
                trade_returns.append(float(pnl) / notional)

        if market_active:
            markets_with_activity += 1

        cash_cents = max(0.0, float(cash_cents))
        equity_curve_cents.append(float(cash_cents))

    equity_np = np.array(equity_curve_cents, dtype=float)
    pnl_np = equity_np - equity_np[0]
    pct_np = np.where(equity_np[0] > 0.0, pnl_np / equity_np[0], 0.0)

    market_returns = np.diff(equity_np) / np.maximum(equity_np[:-1], 1.0)
    mean_return = float(np.mean(market_returns)) if market_returns.size else 0.0
    std_return = float(np.std(market_returns)) if market_returns.size else 0.0
    sharpe = (mean_return / std_return) * (365.0 ** 0.5) if std_return > 1e-12 else 0.0

    outcome_count = int(sum(1 for pnl in pnl_samples_cents if abs(float(pnl)) > 1e-9))
    win_rate = (float(wins) / float(outcome_count) * 100.0) if outcome_count > 0 else 0.0
    loss_rate = (float(losses) / float(outcome_count) * 100.0) if outcome_count > 0 else 0.0

    metrics = {
        "total_markets_evaluated": int(n_paths),
        "total_trades_executed": int(total_trades),
        "entry_trades": int(entry_trades),
        "exit_trades": int(exit_trades),
        "trade_participation_pct": round((float(markets_with_activity) / float(n_paths)) * 100.0, 4),
        "win_rate_pct": round(win_rate, 4),
        "loss_rate_pct": round(loss_rate, 4),
        "average_edge_captured_cents": round(float(np.mean(edge_samples)) if edge_samples else 0.0, 6),
        "max_drawdown_pct": round(_max_drawdown_pct(equity_np) * 100.0, 4),
        "sharpe_ratio": round(float(sharpe), 6),
        "average_decision_time_min": round((float(np.mean(decision_times_sec)) / 60.0) if decision_times_sec else 0.0, 4),
        "ending_bankroll_usd": round(float(cash_cents) / 100.0, 6),
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
