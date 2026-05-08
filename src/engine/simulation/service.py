from __future__ import annotations

import os
import time
from pathlib import Path
from threading import RLock
from typing import Any

from core.asset_context import get_active_asset_context
from core.config import SIMULATION_DEFAULT_STEPS, SIMULATION_MAX_PATHS, SIMULATION_OUTPUT_DIR, SIMULATION_RANDOM_SEED
from core.market_metadata import extract_suggested_strike
from engine.live_pricing import compute_live_pricing_snapshot
from engine.shadow.settings_state import get_shadow_settings_model
from engine.simulation.gbm_engine import GBMConfig, generate_gbm_paths
from engine.simulation.replay import run_monte_carlo_replay
from engine.simulation.visuals import generate_visual_assets
from engine.streamer import get_live_market_info
from feeds.brti_aggregator import get_brti_state

_latest_lock = RLock()
_latest_payload: dict[str, Any] | None = None


def _safe_float(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _safe_int(value: Any) -> int | None:
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value))
        except ValueError:
            return None
    return None


def _resolve_live_inputs(overrides: dict[str, Any]) -> dict[str, Any]:
    settings = get_shadow_settings_model()
    profile = get_active_asset_context().profile
    market_info = get_live_market_info()

    strike = _safe_float(overrides.get("strike_usd"))
    if strike is None:
        strike = _safe_float(extract_suggested_strike(market_info))

    market_ticker = market_info.get("ticker") if isinstance(market_info.get("ticker"), str) else None
    close_time_iso = market_info.get("close_time") if isinstance(market_info.get("close_time"), str) else None

    pricing = compute_live_pricing_snapshot(
        strike=strike,
        market_ticker=market_ticker,
        close_time_iso=close_time_iso,
    )

    spot = _safe_float(overrides.get("start_price"))
    if spot is None:
        spot = _safe_float(pricing.get("spot_index"))
    if spot is None:
        spot = _safe_float(get_brti_state().get("brti"))

    sigma = _safe_float(overrides.get("sigma_annual"))
    if sigma is None:
        sigma = settings.volatility_override
    if sigma is None:
        sigma = _safe_float(pricing.get("sigma_annual"))
    if sigma is None:
        sigma = float(profile.fallback_sigma_annual)

    n_paths = _safe_int(overrides.get("n_paths"))
    if n_paths is None:
        n_paths = int(settings.simulation_n_paths)
    n_paths = max(100, min(int(SIMULATION_MAX_PATHS), int(n_paths)))

    horizon = _safe_int(overrides.get("horizon_seconds"))
    if horizon is None:
        horizon = int(settings.simulation_horizon_seconds)
    horizon = max(60, int(horizon))

    n_steps = _safe_int(overrides.get("n_steps"))
    if n_steps is None:
        n_steps = int(SIMULATION_DEFAULT_STEPS)
    n_steps = max(60, int(n_steps))

    drift = _safe_float(overrides.get("drift_annual"))
    if drift is None:
        drift = 0.0

    return {
        "market_ticker": market_ticker,
        "strike_usd": strike,
        "start_price": spot,
        "sigma_annual": sigma,
        "n_paths": n_paths,
        "horizon_seconds": horizon,
        "n_steps": n_steps,
        "drift_annual": drift,
        "pricing": pricing,
    }


def run_monte_carlo_simulation(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = dict(overrides or {})
    inputs = _resolve_live_inputs(payload)

    if inputs["strike_usd"] is None:
        return {"ok": False, "error": "No strike available for simulation."}
    if inputs["start_price"] is None:
        return {"ok": False, "error": "No live index spot available for simulation."}

    settings = get_shadow_settings_model()

    seed = _safe_int(payload.get("random_seed"))
    if seed is None:
        seed = int(SIMULATION_RANDOM_SEED)

    gbm_config = GBMConfig(
        start_price=float(inputs["start_price"]),
        strike_price=float(inputs["strike_usd"]),
        sigma_annual=max(0.01, float(inputs["sigma_annual"])),
        horizon_seconds=int(inputs["horizon_seconds"]),
        n_paths=int(inputs["n_paths"]),
        n_steps=int(inputs["n_steps"]),
        drift_annual=float(inputs["drift_annual"]),
        random_seed=int(seed),
    )

    paths = generate_gbm_paths(gbm_config)
    replay = run_monte_carlo_replay(
        paths,
        strike_usd=float(inputs["strike_usd"]),
        sigma_annual=float(inputs["sigma_annual"]),
        horizon_seconds=int(inputs["horizon_seconds"]),
        min_edge_cents=float(settings.min_edge_cents),
        slippage_ticks=int(settings.slippage_ticks),
        taker_fee_curve_coeff=float(settings.taker_fee_curve_coeff),
        bankroll_start_usd=float(settings.bankroll_usd),
        trade_size_pct=float(settings.trade_size_pct),
        max_position_usd=float(settings.max_position_usd),
        levy_responsiveness=float(settings.levy_responsiveness),
        settlement_window_seconds=int(get_active_asset_context().profile.settlement_window_seconds),
        random_seed=int(seed),
    )

    visuals = generate_visual_assets(replay)

    output_root = Path(SIMULATION_OUTPUT_DIR).resolve()
    image_urls: dict[str, str] = {}
    for key, abs_path in (visuals.get("images") or {}).items():
        try:
            rel = Path(abs_path).resolve().relative_to(output_root)
            image_urls[key] = f"/output/{rel.as_posix()}"
        except Exception:
            image_urls[key] = ""

    result = {
        "ok": True,
        "generated_ts": time.time(),
        "inputs": {
            "market_ticker": inputs.get("market_ticker"),
            "strike_usd": inputs.get("strike_usd"),
            "start_price": inputs.get("start_price"),
            "sigma_annual": inputs.get("sigma_annual"),
            "n_paths": inputs.get("n_paths"),
            "horizon_seconds": inputs.get("horizon_seconds"),
            "n_steps": inputs.get("n_steps"),
            "drift_annual": inputs.get("drift_annual"),
            "random_seed": seed,
        },
        "metrics": replay.get("metrics") or {},
        "divs": visuals.get("divs") or {},
        "image_urls": image_urls,
        "output_dir": visuals.get("output_dir"),
    }

    with _latest_lock:
        global _latest_payload
        _latest_payload = dict(result)

    return result


def get_latest_simulation_payload() -> dict[str, Any]:
    with _latest_lock:
        if _latest_payload is None:
            return {"ok": False, "error": "No simulation has been generated yet."}
        return dict(_latest_payload)


def resolve_output_artifact(path_suffix: str) -> str | None:
    output_root = Path(SIMULATION_OUTPUT_DIR).resolve()
    candidate = (output_root / path_suffix).resolve()

    if not str(candidate).startswith(str(output_root)):
        return None
    if not candidate.exists() or not candidate.is_file():
        return None
    return os.fspath(candidate)
