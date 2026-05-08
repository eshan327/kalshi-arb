from engine.simulation.gbm_engine import GBMConfig, generate_gbm_paths
from engine.simulation.replay import run_monte_carlo_replay


def test_replay_outputs_required_metrics() -> None:
    paths = generate_gbm_paths(
        GBMConfig(
            start_price=106_000.0,
            strike_price=106_100.0,
            sigma_annual=0.6,
            horizon_seconds=900,
            n_paths=240,
            n_steps=120,
            random_seed=7,
        )
    )

    replay = run_monte_carlo_replay(
        paths,
        strike_usd=106_100.0,
        sigma_annual=0.6,
        horizon_seconds=900,
        min_edge_cents=0.5,
        slippage_ticks=1,
        taker_fee_curve_coeff=7.0,
        bankroll_start_usd=1_000.0,
        trade_size_pct=0.05,
        max_position_usd=50.0,
        levy_responsiveness=1.35,
        random_seed=3,
    )

    metrics = replay["metrics"]
    assert "total_trades_executed" in metrics
    assert "trade_participation_pct" in metrics
    assert "win_rate_pct" in metrics
    assert "loss_rate_pct" in metrics
    assert "average_edge_captured_cents" in metrics
    assert "max_drawdown_pct" in metrics
    assert "sharpe_ratio" in metrics

    assert metrics["total_trades_executed"] > 10
    assert metrics["trade_participation_pct"] > 10.0
    assert 0.0 < metrics["win_rate_pct"] < 100.0
    assert 0.0 < metrics["loss_rate_pct"] < 100.0

    assert isinstance(replay["equity_curve_profit_dollars"], list)
    assert isinstance(replay["equity_curve_return_pct"], list)
    assert isinstance(replay["edge_samples_cents"], list)
    assert isinstance(replay["decision_time_seconds"], list)
    assert isinstance(replay["trade_pnl_cents"], list)
