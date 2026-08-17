from __future__ import annotations

import time
from threading import RLock
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from core.config import KALSHI_ENV


class TradingSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    min_edge_cents: float = Field(default=5.0, ge=0.5, le=25.0)
    max_position_usd: float = Field(default=10.0, ge=1.0, le=50.0)
    max_order_contracts: int = Field(default=5, ge=1, le=25)
    max_daily_loss_usd: float = Field(default=10.0, ge=1.0, le=100.0)
    cash_buffer_usd: float = Field(default=25.0, ge=0.0, le=10_000.0)
    cooldown_seconds: int = Field(default=5, ge=1, le=900)
    slippage_ticks: int = Field(default=1, ge=0, le=5)
    volatility_override: float | None = Field(default=None, ge=0.01, le=5.0)
    volatility_scale: float = Field(default=1.0, ge=0.5, le=2.0)
    use_p_book_hard_gate: bool = False
    p_book_max_divergence: float = Field(default=0.35, ge=0.01, le=0.49)

    @field_validator("volatility_override", mode="before")
    @classmethod
    def normalize_optional_float(cls, value: Any) -> Any:
        if value is None or (
            isinstance(value, str) and value.strip().lower() in {"", "null"}
        ):
            return None
        return value


_DEFAULT_SETTINGS = TradingSettings()
_settings_lock = RLock()
_settings = _DEFAULT_SETTINGS
_settings_updated_ts = time.time()


def get_trading_settings_model() -> TradingSettings:
    with _settings_lock:
        return _settings


def get_trading_settings_snapshot() -> dict[str, Any]:
    with _settings_lock:
        snapshot = _settings.model_dump()
        snapshot["updated_ts"] = _settings_updated_ts
    snapshot["kalshi_env"] = KALSHI_ENV
    return snapshot


def reset_trading_settings() -> dict[str, Any]:
    global _settings, _settings_updated_ts
    with _settings_lock:
        _settings = _DEFAULT_SETTINGS
        _settings_updated_ts = time.time()
    return get_trading_settings_snapshot()


def update_trading_settings(
    payload: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    global _settings, _settings_updated_ts
    if not isinstance(payload, dict):
        return get_trading_settings_snapshot(), ["Payload must be a JSON object."]

    with _settings_lock:
        try:
            updated = TradingSettings.model_validate(
                {**_settings.model_dump(), **payload}
            )
        except ValidationError as exc:
            errors = [
                f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
                for error in exc.errors()
            ]
            return get_trading_settings_snapshot(), errors
        _settings = updated
        _settings_updated_ts = time.time()
    return get_trading_settings_snapshot(), []
