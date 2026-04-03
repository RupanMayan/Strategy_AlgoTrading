"""
Black-76 Implied Volatility Computation for Nifty Options.

Used by the backtest engine to compute historical IV from 1-min candle data.
Black-76 is appropriate for index options where the underlying is a futures/forward price.
"""
from __future__ import annotations

import math
from scipy.stats import norm


def black76_call_price(F: float, K: float, T: float, r: float, sigma: float) -> float:
    """Black-76 call price.

    Args:
        F: Forward price (spot * exp(r*T))
        K: Strike price
        T: Time to expiry in years
        r: Risk-free rate (annualized)
        sigma: Volatility (annualized, decimal e.g. 0.15 for 15%)

    Returns:
        Theoretical call price.
    """
    if T <= 0 or sigma <= 0:
        return max(F - K, 0.0)  # Intrinsic value

    sqrt_T = math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * sigma * sigma * T) / (sigma * sqrt_T)
    d2 = d1 - sigma * sqrt_T

    return math.exp(-r * T) * (F * norm.cdf(d1) - K * norm.cdf(d2))


def black76_put_price(F: float, K: float, T: float, r: float, sigma: float) -> float:
    """Black-76 put price via put-call parity."""
    if T <= 0 or sigma <= 0:
        return max(K - F, 0.0)

    sqrt_T = math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * sigma * sigma * T) / (sigma * sqrt_T)
    d2 = d1 - sigma * sqrt_T

    return math.exp(-r * T) * (K * norm.cdf(-d2) - F * norm.cdf(-d1))


def black76_vega(F: float, K: float, T: float, r: float, sigma: float) -> float:
    """Vega (dC/dsigma) under Black-76. Same for call and put."""
    if T <= 0 or sigma <= 0:
        return 0.0

    sqrt_T = math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * sigma * sigma * T) / (sigma * sqrt_T)

    return math.exp(-r * T) * F * sqrt_T * norm.pdf(d1)


def implied_vol_black76(
    market_price: float,
    spot: float,
    K: float,
    T: float,
    r: float = 0.065,
    option_type: str = "call",
    max_iter: int = 50,
    tol: float = 0.01,
) -> float | None:
    """Solve for implied volatility using Newton-Raphson.

    Args:
        market_price: Observed option price.
        spot: Underlying spot price.
        K: Strike price.
        T: Time to expiry in years.
        r: Risk-free rate.
        option_type: 'call' or 'put'.
        max_iter: Maximum Newton-Raphson iterations.
        tol: Price tolerance for convergence.

    Returns:
        Annualized IV as decimal (e.g. 0.15 for 15%), or None if no convergence.
    """
    if market_price <= 0 or spot <= 0 or K <= 0:
        return None

    # Floor T for DTE=0 to avoid division by zero
    if T <= 0:
        T = 0.5 / 365.0  # Half a day

    F = spot * math.exp(r * T)
    price_fn = black76_call_price if option_type == "call" else black76_put_price

    # Check intrinsic: price must exceed intrinsic value
    intrinsic = max(F - K, 0.0) if option_type == "call" else max(K - F, 0.0)
    discounted_intrinsic = math.exp(-r * T) * intrinsic
    if market_price < discounted_intrinsic * 0.95:  # Allow small tolerance
        return None

    # Initial guess based on Brenner-Subrahmanyam approximation
    sigma = math.sqrt(2 * math.pi / T) * (market_price / F)
    sigma = max(0.05, min(sigma, 2.0))  # Clamp to [5%, 200%]

    for _ in range(max_iter):
        theo = price_fn(F, K, T, r, sigma)
        diff = theo - market_price

        if abs(diff) < tol:
            return sigma

        vega = black76_vega(F, K, T, r, sigma)
        if vega < 1e-10:  # Vega too small, can't converge
            break

        sigma -= diff / vega
        sigma = max(0.01, min(sigma, 5.0))  # Keep in [1%, 500%]

    return None  # No convergence


def compute_entry_iv(
    ce_price: float,
    pe_price: float,
    spot: float,
    dte: int,
    r: float = 0.065,
    strike_rounding: int = 50,
) -> float | None:
    """Compute annualized IV from ATM CE/PE prices.

    Tries CE first (more stable for ATM), falls back to PE.

    Args:
        ce_price: ATM call option price.
        pe_price: ATM put option price.
        spot: Underlying spot price.
        dte: Days to expiry (trading days).
        r: Risk-free rate.
        strike_rounding: Strike interval for ATM rounding.

    Returns:
        Annualized IV as percentage (e.g. 12.5 for 12.5%), or None.
    """
    if spot <= 0 or ce_price <= 0 or pe_price <= 0:
        return None

    # Convert DTE (trading days) to calendar time
    T = max(dte * 365.0 / 252.0, 0.5) / 365.0  # Floor at half a day

    # ATM strike
    K = round(spot / strike_rounding) * strike_rounding

    # Try CE first
    iv = implied_vol_black76(ce_price, spot, K, T, r, option_type="call")

    # Fallback to PE
    if iv is None:
        iv = implied_vol_black76(pe_price, spot, K, T, r, option_type="put")

    if iv is None:
        return None

    return iv * 100.0  # Convert to percentage
