"""
Binary fair value for index TWAP over the last 60s versus strike K (Kalshi-style settlement).

- **More than 60s to expiry:** Levy moment-matching (lognormal approximation to the arithmetic
  average of 60 future spots) — equivalent in spirit to Turnbull–Wakeman / industry Asian
  approximations; probability uses the natural ``N(d2)`` analogue on the matched law.
- **Inside the last 60s:** locked-in samples plus a moment-matched distribution for the
  remaining discrete fixes.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

SECONDS_PER_YEAR = 365.25 * 24 * 3600.0
_SETTLEMENT_SECONDS_DEFAULT = 60


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _clamp_prob(p: float) -> float:
    eps = 1e-12
    return min(1.0 - eps, max(eps, p))


@dataclass(frozen=True)
class AsianBinaryPricerResult:
    p_model: float
    regime: Literal["levy_tw", "collapsed", "terminal"]
    sigma_eff: float | None
    detail: dict[str, float | int | str | None]


def _fixing_times_years(seconds_to_expiry: float, n: int) -> list[float]:
    """
    Seconds from *now* until each of the n TWAP samples inside the settlement window.

    Kalshi's final-minute CF accumulation uses (expiry-n, expiry]: the start-boundary
    tick is excluded and the close tick is included.
    """
    tau = float(seconds_to_expiry)
    out: list[float] = []
    for j in range(n):
        sec_from_now = (tau - n) + j + 1
        out.append(max(sec_from_now, 0.0) / SECONDS_PER_YEAR)
    return out


def _levy_moment_match_m2(
    S0: float, sigma_annual: float, t_years: list[float]
) -> tuple[float, float]:
    """Returns (M1, M2) for A = (1/n)Σ S_{t_i} under GBM with r=0."""
    n = len(t_years)
    sig2 = sigma_annual * sigma_annual
    M1 = S0
    # t_years is sorted. In the covariance matrix min(t_i, t_j), the
    # value t_k appears 2(n-k)-1 times, so the double sum is exactly O(n).
    acc = sum(
        (2 * (n - k) - 1) * math.exp(sig2 * t_years[k])
        for k in range(n)
    )
    M2 = (S0 * S0) / (n * n) * acc
    return M1, M2


def _prob_moment_matched_lognormal(
    mean: float, second_moment: float, threshold: float
) -> tuple[float, float, float | None]:
    if threshold <= 0:
        return 1.0, 0.0, None
    ratio = second_moment / (mean * mean) if mean > 0 else 0.0
    if ratio <= 1.0 or not math.isfinite(ratio):
        return (1.0 if mean >= threshold else 0.0), 0.0, None

    sigma2 = math.log(ratio)
    sigma = math.sqrt(sigma2)
    d2 = (math.log(mean / threshold) - 0.5 * sigma2) / sigma
    return norm_cdf(d2), sigma, d2


def prob_levy_tw_binary(
    S0: float,
    strike: float,
    sigma_annual: float,
    seconds_to_expiry: float,
    n_fixes: int = _SETTLEMENT_SECONDS_DEFAULT,
) -> AsianBinaryPricerResult:
    """
    P(TWAP > K) before the settlement window starts: lognormal matched to first two moments
    of the discrete arithmetic average (Levy-style).
    """
    if strike <= 0 or S0 <= 0 or sigma_annual <= 0:
        return AsianBinaryPricerResult(
            p_model=0.5,
            regime="levy_tw",
            sigma_eff=None,
            detail={"reason": "bad_inputs"},
        )

    tau = float(seconds_to_expiry)
    if tau <= n_fixes:
        return AsianBinaryPricerResult(
            p_model=0.5,
            regime="levy_tw",
            sigma_eff=None,
            detail={"reason": "use_collapsed_branch"},
        )

    t_years = _fixing_times_years(tau, n_fixes)
    M1, M2 = _levy_moment_match_m2(S0, sigma_annual, t_years)

    p, sigma_a, d2 = _prob_moment_matched_lognormal(M1, M2, strike)
    if d2 is None:
        return AsianBinaryPricerResult(
            p_model=_clamp_prob(p),
            regime="levy_tw",
            sigma_eff=0.0,
            detail={"M1": M1, "M2": M2, "note": "degenerate_variance"},
        )

    return AsianBinaryPricerResult(
        p_model=_clamp_prob(p),
        regime="levy_tw",
        sigma_eff=sigma_a,
        detail={"M1": M1, "M2": M2, "d2": d2, "n_fixes": n_fixes},
    )


def prob_collapsed_variance_binary(
    strike: float,
    sigma_annual: float,
    *,
    n: int,
    k: int,
    mean_known_samples: float | None,
    mu_fwd: float,
) -> AsianBinaryPricerResult:
    """
    Inside the settlement window:

    The observed sum is fixed. The remaining arithmetic average is matched to a
    lognormal distribution using the covariance of every remaining one-second fix.
    """
    if strike <= 0 or sigma_annual <= 0 or mu_fwd <= 0:
        return AsianBinaryPricerResult(
            p_model=0.5,
            regime="collapsed",
            sigma_eff=None,
            detail={"reason": "bad_inputs"},
        )

    k = max(0, min(k, n))
    if k == n:
        if mean_known_samples is None:
            avg = mu_fwd
        else:
            avg = mean_known_samples
        p = 1.0 if avg >= strike else 0.0
        return AsianBinaryPricerResult(
            p_model=_clamp_prob(p),
            regime="terminal",
            sigma_eff=0.0,
            detail={"k": k, "n": n, "avg": avg},
        )

    rem = n - k
    if rem <= 0:
        p = 1.0 if (mean_known_samples or mu_fwd) >= strike else 0.0
        return AsianBinaryPricerResult(
            p_model=_clamp_prob(p),
            regime="terminal",
            sigma_eff=0.0,
            detail={"k": k, "n": n},
        )

    known_sum = (
        k * float(mean_known_samples) if k and mean_known_samples is not None else 0.0
    )
    required_future_avg = (n * strike - known_sum) / rem
    remaining_times = [second / SECONDS_PER_YEAR for second in range(1, rem + 1)]
    mean, second_moment = _levy_moment_match_m2(mu_fwd, sigma_annual, remaining_times)
    p, sigma_eff, d2 = _prob_moment_matched_lognormal(
        mean, second_moment, required_future_avg
    )

    return AsianBinaryPricerResult(
        p_model=_clamp_prob(p),
        regime="collapsed",
        sigma_eff=sigma_eff,
        detail={
            "k": k,
            "n": n,
            "rem": rem,
            "required_future_avg": required_future_avg,
            "future_M1": mean,
            "future_M2": second_moment,
            "d2": d2,
        },
    )
