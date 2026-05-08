from engine.shadow.settings_state import (
    get_shadow_settings_model,
    reset_shadow_settings,
    resolve_effective_mode,
    update_shadow_settings,
)


def test_live_mode_requires_live_env() -> None:
    mode, reason = resolve_effective_mode("live", env_mode="paper")
    assert mode == "paper"
    assert reason == "live_requires_env_live"


def test_settings_update_and_validation() -> None:
    reset_shadow_settings()

    snapshot, errors = update_shadow_settings(
        {
            "execution_mode": "paper",
            "min_edge_cents": 0.5,
            "trade_size_pct": 0.05,
        }
    )
    assert errors == []
    assert snapshot["execution_mode"] == "paper"
    assert snapshot["min_edge_cents"] == 0.5

    _, invalid_errors = update_shadow_settings({"trade_size_pct": 2.0})
    assert invalid_errors


def test_default_edge_is_liberal_for_demo() -> None:
    reset_shadow_settings()
    settings = get_shadow_settings_model()
    assert settings.min_edge_cents == 0.1
