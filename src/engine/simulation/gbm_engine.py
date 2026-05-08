from __future__ import annotations

from dataclasses import dataclass

import numpy as np

SECONDS_PER_YEAR = 365.25 * 24.0 * 3600.0


@dataclass(frozen=True)
class GBMConfig:
    start_price: float
    strike_price: float | None
    sigma_annual: float
    horizon_seconds: int
    n_paths: int
    n_steps: int
    drift_annual: float = 0.0
    random_seed: int | None = None


def _draw_start_prices(config: GBMConfig, rng: np.random.Generator, n_paths: int) -> np.ndarray:
    strike = float(config.strike_price) if isinstance(config.strike_price, (int, float)) else None

    if strike is not None and strike > 0.0:
        # Mixture: ITM, ATM, OTM around strike to create truly independent market contexts.
        regimes = rng.choice(np.array([-1, 0, 1]), size=n_paths, p=[0.34, 0.32, 0.34])
        offsets = rng.normal(loc=0.0, scale=0.0014, size=n_paths)
        offsets += np.where(regimes == 1, rng.normal(loc=0.0045, scale=0.0012, size=n_paths), 0.0)
        offsets += np.where(regimes == -1, rng.normal(loc=-0.0045, scale=0.0012, size=n_paths), 0.0)
        offsets = np.clip(offsets, -0.012, 0.012)
        starts = strike * (1.0 + offsets)
    else:
        base = max(1e-6, float(config.start_price))
        offsets = rng.normal(loc=0.0, scale=0.003, size=n_paths)
        starts = base * (1.0 + offsets)

    return np.maximum(starts, 1e-6)


def generate_gbm_paths(config: GBMConfig) -> np.ndarray:
    n_paths = max(1, int(config.n_paths))
    n_steps = max(2, int(config.n_steps))
    horizon_seconds = max(1.0, float(config.horizon_seconds))

    sigma = max(1e-6, float(config.sigma_annual))
    mu = float(config.drift_annual)

    dt_years = (horizon_seconds / float(n_steps)) / SECONDS_PER_YEAR
    sqrt_dt = dt_years ** 0.5

    rng = np.random.default_rng(config.random_seed)
    starts = _draw_start_prices(config, rng, n_paths)
    z = rng.standard_normal(size=(n_paths, n_steps))
    increments = (mu - 0.5 * sigma * sigma) * dt_years + sigma * sqrt_dt * z

    log_paths = np.cumsum(increments, axis=1)
    paths = starts[:, None] * np.exp(log_paths)
    starts_col = starts.reshape(n_paths, 1)
    return np.concatenate([starts_col, paths], axis=1)
