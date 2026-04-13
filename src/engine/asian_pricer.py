"""
Binary fair value for index TWAP over the last 60s versus strike K (Kalshi-style settlement).

- **More than 60s to expiry:** Levy moment-matching (lognormal approximation to the arithmetic
  average of 60 future spots) — equivalent in spirit to Turnbull–Wakeman / industry Asian
  approximations; probability uses the natural ``N(d2)`` analogue on the matched law.
- **Inside the last 60s:** collapsed-variance model: locked-in samples plus Gaussian uncertainty
  on the remaining seconds (user spec).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from engine.twap import TwapCalculator

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

    Window ends at expiry; samples at end of seconds 1..n inside the window, matching
    :class:`TwapCalculator` discrete reconstruction.
    """
    tau = float(seconds_to_expiry)
    out: list[float] = []
    for j in range(1, n + 1):
        sec_from_now = (tau - n) + j
        out.append(max(sec_from_now, 0.0) / SECONDS_PER_YEAR)
    return out


def _levy_moment_match_m2(S0: float, sigma_annual: float, t_years: list[float]) -> tuple[float, float]:
    """Returns (M1, M2) for A = (1/n)Σ S_{t_i} under GBM with r=0."""
    n = len(t_years)
    sig2 = sigma_annual * sigma_annual
    M1 = S0
    acc = 0.0
    for i in range(n):
        for j in range(n):
            acc += math.exp(sig2 * min(t_years[i], t_years[j]))
    M2 = (S0 * S0) / (n * n) * acc
    return M1, M2


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

    ratio = M2 / (M1 * M1) if M1 > 0 else 0.0
    if ratio <= 1.0 or not math.isfinite(ratio):
        p = 1.0 if M1 > strike else 0.0 if M1 < strike else 0.5
        return AsianBinaryPricerResult(
            p_model=_clamp_prob(p),
            regime="levy_tw",
            sigma_eff=0.0,
            detail={"M1": M1, "M2": M2, "note": "degenerate_variance"},
        )

    sigma_a2 = math.log(ratio)
    sigma_a = math.sqrt(sigma_a2)
    # Matched lognormal: ln A ~ N(ln(M1) - σ_a²/2, σ_a²) ⇒ P(A > K) = N(d2)
    d2 = (math.log(M1 / strike) - 0.5 * sigma_a2) / sigma_a
    p = norm_cdf(d2)

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

    P = N( ( (k/n) S̄_k + ((n-k)/n) μ_fwd - K ) / ( ((n-k)/n) σ √(Δt) ) )

    with Δt = (n-k) / SECONDS_PER_YEAR (remaining window length in year fraction).
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
        p = 1.0 if avg > strike else 0.0 if avg < strike else 0.5
        return AsianBinaryPricerResult(
            p_model=_clamp_prob(p),
            regime="terminal",
            sigma_eff=0.0,
            detail={"k": k, "n": n, "avg": avg},
        )

    rem = n - k
    if rem <= 0:
        p = 1.0 if (mean_known_samples or mu_fwd) > strike else 0.0
        return AsianBinaryPricerResult(
            p_model=_clamp_prob(p),
            regime="terminal",
            sigma_eff=0.0,
            detail={"k": k, "n": n},
        )

    if k == 0 or mean_known_samples is None:
        s_bar = 0.0
        w_k = 0.0
    else:
        s_bar = mean_known_samples
        w_k = k / n

    w_rem = rem / n
    mu_avg = w_k * s_bar + w_rem * mu_fwd

    delta_t = rem / SECONDS_PER_YEAR
    denom = w_rem * sigma_annual * math.sqrt(delta_t)
    if denom <= 1e-18 * max(1.0, abs(mu_avg)):
        p = 1.0 if mu_avg > strike else 0.0 if mu_avg < strike else 0.5
        return AsianBinaryPricerResult(
            p_model=_clamp_prob(p),
            regime="collapsed",
            sigma_eff=0.0,
            detail={"mu_avg": mu_avg, "note": "zero_denom"},
        )

    z = (mu_avg - strike) / denom
    p = norm_cdf(z)

    return AsianBinaryPricerResult(
        p_model=_clamp_prob(p),
        regime="collapsed",
        sigma_eff=sigma_annual * math.sqrt(delta_t) * w_rem,
        detail={"k": k, "n": n, "z": z, "mu_avg": mu_avg, "rem": rem},
    )


def price_btwap_binary(
    spot: float,
    strike: float,
    sigma_annual: float,
    seconds_to_expiry: float,
    twap: TwapCalculator | None,
    *,
    settlement_seconds: int = _SETTLEMENT_SECONDS_DEFAULT,
    mu_fwd: float | None = None,
) -> AsianBinaryPricerResult:
    """
    Dispatch: ``seconds_to_expiry > settlement_seconds`` → Levy/TW branch; else collapsed.

    ``twap`` should be the live :class:`TwapCalculator` (window started at settlement window open).
    ``mu_fwd`` defaults to ``spot`` (forward proxy for remaining BRTI samples).
    """
    fwd = float(mu_fwd) if mu_fwd is not None else float(spot)
    tau = float(seconds_to_expiry)
    n = int(settlement_seconds)

    if tau > n:
        return prob_levy_tw_binary(spot, strike, sigma_annual, tau, n_fixes=n)

    k = 0
    mean_k: float | None = None
    if twap is not None:
        k = twap.seconds_elapsed()
        samples = twap.discrete_samples()
        if samples:
            mean_k = sum(samples) / len(samples)

    return prob_collapsed_variance_binary(
        strike,
        sigma_annual,
        n=n,
        k=k,
        mean_known_samples=mean_k,
        mu_fwd=fwd,
    )
