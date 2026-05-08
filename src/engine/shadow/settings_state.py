from __future__ import annotations

import time
from dataclasses import asdict, dataclass, replace
from threading import RLock
from typing import Any, Literal

from core.config import (
    EXECUTION_MODE,
    PAPER_SIM_STARTING_CASH_CENTS,
    SIMULATION_DEFAULT_N_PATHS,
    SIMULATION_HORIZON_SECONDS,
    SIMULATION_MAX_PATHS,
)

ExecutionMode = Literal["observe", "paper", "live"]


@dataclass(frozen=True)
class ShadowSettings:
    strategy_enabled: bool
    execution_mode: ExecutionMode
    min_edge_cents: float
    trade_size_pct: float
    max_position_usd: float
    slippage_ticks: int
    volatility_override: float | None
    levy_responsiveness: float
    use_p_book_hard_gate: bool
    p_book_min_quality: float
    p_book_max_divergence: float
    taker_fee_curve_coeff: float
    bankroll_usd: float
    simulation_n_paths: int
    simulation_horizon_seconds: int


_DEFAULT_EXECUTION_MODE = EXECUTION_MODE if EXECUTION_MODE in {"observe", "paper", "live"} else "observe"

_DEFAULT_SETTINGS = ShadowSettings(
    strategy_enabled=True,
    execution_mode=_DEFAULT_EXECUTION_MODE,
    min_edge_cents=0.5,
    trade_size_pct=0.05,
    max_position_usd=50.0,
    slippage_ticks=1,
    volatility_override=None,
    levy_responsiveness=1.35,
    use_p_book_hard_gate=False,
    p_book_min_quality=0.35,
    p_book_max_divergence=0.45,
    taker_fee_curve_coeff=7.0,
    bankroll_usd=max(10.0, float(PAPER_SIM_STARTING_CASH_CENTS) / 100.0),
    simulation_n_paths=max(100, min(int(SIMULATION_MAX_PATHS), int(SIMULATION_DEFAULT_N_PATHS))),
    simulation_horizon_seconds=max(60, int(SIMULATION_HORIZON_SECONDS)),
)

_settings_lock = RLock()
_settings = _DEFAULT_SETTINGS
_settings_updated_ts = time.time()


def _normalize_mode(value: Any, default: str) -> str:
    mode = str(value if value is not None else default).strip().lower()
    if mode in {"observe", "paper", "live"}:
        return mode
    return str(default)


def _coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "y", "on"}:
            return True
        if normalized in {"0", "false", "no", "n", "off"}:
            return False
    return bool(default)


def _coerce_float(value: Any, default: float) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return float(default)
    return float(default)


def _coerce_int(value: Any, default: int) -> int:
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value.strip()))
        except ValueError:
            return int(default)
    return int(default)


def resolve_effective_mode(
    requested_mode: str | None = None,
    *,
    env_mode: str | None = None,
) -> tuple[str, str | None]:
    """Hybrid rule: API can toggle observe/paper always; live requires env mode live."""
    env_resolved = _normalize_mode(env_mode, _DEFAULT_EXECUTION_MODE)
    req = _normalize_mode(requested_mode, env_resolved)

    if req == "live" and env_resolved != "live":
        return env_resolved, "live_requires_env_live"

    if req in {"observe", "paper"}:
        return req, None

    return req, None


def get_shadow_settings_model() -> ShadowSettings:
    with _settings_lock:
        return _settings


def get_shadow_settings_snapshot() -> dict[str, Any]:
    with _settings_lock:
        snapshot = asdict(_settings)
        snapshot["updated_ts"] = _settings_updated_ts

    effective_mode, mode_reason = resolve_effective_mode(snapshot.get("execution_mode"))
    snapshot["effective_mode"] = effective_mode
    snapshot["mode_reason"] = mode_reason
    snapshot["env_execution_mode"] = _DEFAULT_EXECUTION_MODE
    return snapshot


def reset_shadow_settings() -> dict[str, Any]:
    global _settings, _settings_updated_ts
    with _settings_lock:
        _settings = _DEFAULT_SETTINGS
        _settings_updated_ts = time.time()
    return get_shadow_settings_snapshot()


def update_shadow_settings(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    global _settings, _settings_updated_ts
    if not isinstance(payload, dict):
        return get_shadow_settings_snapshot(), ["Payload must be a JSON object."]

    errors: list[str] = []
    with _settings_lock:
        updated = _settings

        for key, value in payload.items():
            if not hasattr(updated, key):
                errors.append(f"Unknown setting '{key}'.")
                continue

            if key == "strategy_enabled":
                updated = replace(updated, strategy_enabled=_coerce_bool(value, updated.strategy_enabled))
                continue

            if key == "execution_mode":
                updated = replace(updated, execution_mode=_normalize_mode(value, updated.execution_mode))
                continue

            if key == "min_edge_cents":
                parsed = _coerce_float(value, updated.min_edge_cents)
                if not (0.0 <= parsed <= 25.0):
                    errors.append("min_edge_cents must be in [0, 25].")
                else:
                    updated = replace(updated, min_edge_cents=parsed)
                continue

            if key == "trade_size_pct":
                parsed = _coerce_float(value, updated.trade_size_pct)
                if not (0.001 <= parsed <= 1.0):
                    errors.append("trade_size_pct must be in [0.001, 1.0].")
                else:
                    updated = replace(updated, trade_size_pct=parsed)
                continue

            if key == "max_position_usd":
                parsed = _coerce_float(value, updated.max_position_usd)
                if not (1.0 <= parsed <= 50_000.0):
                    errors.append("max_position_usd must be in [1, 50000].")
                else:
                    updated = replace(updated, max_position_usd=parsed)
                continue

            if key == "slippage_ticks":
                parsed = _coerce_int(value, updated.slippage_ticks)
                if not (0 <= parsed <= 10):
                    errors.append("slippage_ticks must be in [0, 10].")
                else:
                    updated = replace(updated, slippage_ticks=parsed)
                continue

            if key == "volatility_override":
                if value is None or (
                    isinstance(value, str) and value.strip().lower() in {"", "null"}
                ):
                    updated = replace(updated, volatility_override=None)
                else:
                    parsed = _coerce_float(value, updated.volatility_override or 0.0)
                    if not (0.01 <= parsed <= 5.0):
                        errors.append("volatility_override must be null or in [0.01, 5.0].")
                    else:
                        updated = replace(updated, volatility_override=parsed)
                continue

            if key == "levy_responsiveness":
                parsed = _coerce_float(value, updated.levy_responsiveness)
                if not (0.1 <= parsed <= 3.0):
                    errors.append("levy_responsiveness must be in [0.1, 3.0].")
                else:
                    updated = replace(updated, levy_responsiveness=parsed)
                continue

            if key == "use_p_book_hard_gate":
                updated = replace(updated, use_p_book_hard_gate=_coerce_bool(value, updated.use_p_book_hard_gate))
                continue

            if key == "p_book_min_quality":
                parsed = _coerce_float(value, updated.p_book_min_quality)
                if not (0.0 <= parsed <= 1.0):
                    errors.append("p_book_min_quality must be in [0, 1].")
                else:
                    updated = replace(updated, p_book_min_quality=parsed)
                continue

            if key == "p_book_max_divergence":
                parsed = _coerce_float(value, updated.p_book_max_divergence)
                if not (0.01 <= parsed <= 0.49):
                    errors.append("p_book_max_divergence must be in [0.01, 0.49].")
                else:
                    updated = replace(updated, p_book_max_divergence=parsed)
                continue

            if key == "taker_fee_curve_coeff":
                parsed = _coerce_float(value, updated.taker_fee_curve_coeff)
                if not (0.0 <= parsed <= 25.0):
                    errors.append("taker_fee_curve_coeff must be in [0, 25].")
                else:
                    updated = replace(updated, taker_fee_curve_coeff=parsed)
                continue

            if key == "bankroll_usd":
                parsed = _coerce_float(value, updated.bankroll_usd)
                if not (10.0 <= parsed <= 1_000_000.0):
                    errors.append("bankroll_usd must be in [10, 1000000].")
                else:
                    updated = replace(updated, bankroll_usd=parsed)
                continue

            if key == "simulation_n_paths":
                parsed = _coerce_int(value, updated.simulation_n_paths)
                if not (100 <= parsed <= int(SIMULATION_MAX_PATHS)):
                    errors.append(f"simulation_n_paths must be in [100, {int(SIMULATION_MAX_PATHS)}].")
                else:
                    updated = replace(updated, simulation_n_paths=parsed)
                continue

            if key == "simulation_horizon_seconds":
                parsed = _coerce_int(value, updated.simulation_horizon_seconds)
                if not (60 <= parsed <= 86_400):
                    errors.append("simulation_horizon_seconds must be in [60, 86400].")
                else:
                    updated = replace(updated, simulation_horizon_seconds=parsed)
                continue

        if not errors:
            _settings = updated
            _settings_updated_ts = time.time()

    return get_shadow_settings_snapshot(), errors
