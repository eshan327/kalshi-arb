from __future__ import annotations

from dataclasses import dataclass

import numpy as np

SECONDS_PER_YEAR = 365.25 * 24.0 * 3600.0


@dataclass(frozen=True)
class GBMConfig:
    start_price: float
    sigma_annual: float
    horizon_seconds: int
    n_paths: int
    n_steps: int
    drift_annual: float = 0.0
    random_seed: int | None = None


def generate_gbm_paths(config: GBMConfig) -> np.ndarray:
    n_paths = max(1, int(config.n_paths))
    n_steps = max(2, int(config.n_steps))
    horizon_seconds = max(1.0, float(config.horizon_seconds))

    s0 = max(1e-6, float(config.start_price))
    sigma = max(1e-6, float(config.sigma_annual))
    mu = float(config.drift_annual)

    dt_years = (horizon_seconds / float(n_steps)) / SECONDS_PER_YEAR
    sqrt_dt = dt_years ** 0.5

    rng = np.random.default_rng(config.random_seed)
    z = rng.standard_normal(size=(n_paths, n_steps))
    increments = (mu - 0.5 * sigma * sigma) * dt_years + sigma * sqrt_dt * z

    log_paths = np.cumsum(increments, axis=1)
    paths = s0 * np.exp(log_paths)
    starts = np.full((n_paths, 1), s0)
    return np.concatenate([starts, paths], axis=1)
