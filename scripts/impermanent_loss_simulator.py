#!/usr/bin/env python3
"""Simulate impermanent loss for an unlevered, fee-free 50/50 constant-product pool.

Run: python3 scripts/impermanent_loss_simulator.py

The quoted asset starts at price 1 and follows geometric Brownian motion:
S(t+dt) = S(t) * exp((drift - volatility**2 / 2)*dt + volatility*sqrt(dt)*Z).
Drift and volatility are annualized fractions; a year is 365 days. Each path
uses independent normal increments. Only the terminal price affects IL, but
every configured step is simulated. Results are relative to holding the same
initial quantities of the two assets. VaR is the positive IL shortfall at the
given percentile, not the total portfolio's market VaR. Breakeven APY is the
constant compounded LP yield needed over the horizon to match that hold.
Trading fees, gas, rebalancing, and changes to the quote asset are excluded.
"""

import argparse
import bisect
import json
import math
import random
from dataclasses import asdict, dataclass


DAYS_PER_YEAR = 365.0
LOSS_BUCKET_EDGES_PCT = (0, 1, 2, 5, 10, 20, 50, 100)


def _finite_number(value, name, *, positive=False, nonnegative=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    try:
        result = float(value)
    except OverflowError as error:
        raise ValueError(f"{name} must be a finite number") from error
    if not math.isfinite(result):
        raise ValueError(f"{name} must be a finite number")
    if positive and result <= 0 or nonnegative and result < 0:
        raise ValueError(f"{name} is outside its permitted range")
    return result


@dataclass(frozen=True)
class SimulationConfig:
    paths: int = 10_000
    horizon_days: float = 365.0
    steps: int = 365
    volatility: float = 0.8
    drift: float = 0.0
    seed: int = 42

    def __post_init__(self):
        for name in ("paths", "steps"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int):
            raise ValueError("seed must be an integer")
        _finite_number(self.horizon_days, "horizon_days", positive=True)
        _finite_number(self.volatility, "volatility", nonnegative=True)
        _finite_number(self.drift, "drift")


def _log_lp_retention(log_price_ratio):
    """Log of LP value / hold value, stable for large reciprocal moves."""
    distance = abs(log_price_ratio)
    return math.log(2) - distance / 2 - math.log1p(math.exp(-distance))


def impermanent_loss(price_ratio):
    """Return fractional LP shortfall; extreme ratios may round to -1."""
    ratio = _finite_number(price_ratio, "price_ratio", positive=True)
    return math.expm1(_log_lp_retention(math.log(ratio)))


def breakeven_apy(il, horizon_days):
    """Annual compounded yield required to offset fractional IL over the horizon."""
    il = _finite_number(il, "il")
    days = _finite_number(horizon_days, "horizon_days", positive=True)
    if not -1 < il <= 0:
        raise ValueError("il must be greater than -1 and no greater than 0")
    try:
        return math.expm1(-math.log1p(il) * DAYS_PER_YEAR / days)
    except OverflowError as error:
        raise ValueError("breakeven APY exceeds finite floating-point range") from error


def quantile(sorted_values, probability):
    """Linearly interpolate the p*(n-1) sample quantile of sorted values."""
    if not sorted_values:
        raise ValueError("quantile requires at least one value")
    p = _finite_number(probability, "probability", nonnegative=True)
    if p > 1:
        raise ValueError("probability must be no greater than 1")
    position = p * (len(sorted_values) - 1)
    lower = int(position)
    fraction = position - lower
    return sorted_values[lower] + fraction * (sorted_values[min(lower + 1, len(sorted_values) - 1)]
                                               - sorted_values[lower])


def _distribution(values):
    ordered = sorted(values)
    return {
        "min": ordered[0],
        "mean": math.fsum(ordered) / len(ordered),
        "p05": quantile(ordered, 0.05),
        "p50": quantile(ordered, 0.50),
        "p95": quantile(ordered, 0.95),
        "p99": quantile(ordered, 0.99),
        "max": ordered[-1],
    }


def _loss_histogram(losses_pct):
    counts = [0] * (len(LOSS_BUCKET_EDGES_PCT) - 1)
    for loss in losses_pct:
        counts[min(bisect.bisect_right(LOSS_BUCKET_EDGES_PCT, loss) - 1, len(counts) - 1)] += 1
    return [
        {"from_pct": lower, "to_pct": upper, "count": count}
        for lower, upper, count in zip(LOSS_BUCKET_EDGES_PCT, LOSS_BUCKET_EDGES_PCT[1:], counts)
    ]


def simulate(config=SimulationConfig()):
    """Run independent paths and return summary distributions in percentage points."""
    if not isinstance(config, SimulationConfig):
        raise TypeError("config must be a SimulationConfig")
    rng = random.Random(config.seed)
    step_years = config.horizon_days / DAYS_PER_YEAR / config.steps
    deterministic_step = (config.drift - config.volatility ** 2 / 2) * step_years
    random_step = config.volatility * math.sqrt(step_years)
    il_pct = []
    losses_pct = []
    apy_pct = []
    for _ in range(config.paths):
        log_price_ratio = 0.0
        for _ in range(config.steps):
            log_price_ratio += deterministic_step + random_step * rng.gauss(0, 1)
        if not math.isfinite(log_price_ratio):
            raise ValueError("simulated price path is outside finite floating-point range")
        log_retention = _log_lp_retention(log_price_ratio)
        il = math.expm1(log_retention)
        try:
            apy = math.expm1(-log_retention * DAYS_PER_YEAR / config.horizon_days)
        except OverflowError as error:
            raise ValueError("breakeven APY exceeds finite floating-point range") from error
        if not math.isfinite(apy):
            raise ValueError("breakeven APY exceeds finite floating-point range")
        il_pct.append(il * 100)
        losses_pct.append(-il * 100)
        apy_pct.append(apy * 100)

    ordered_losses = sorted(losses_pct)
    return {
        "model": "50/50 constant-product versus hold; geometric Brownian price paths",
        "parameters": asdict(config),
        "il_distribution_pct": _distribution(il_pct),
        "loss_histogram_pct": _loss_histogram(losses_pct),
        "var_pct": {"95": quantile(ordered_losses, 0.95), "99": quantile(ordered_losses, 0.99)},
        "breakeven_apy_distribution_pct": _distribution(apy_pct),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--paths", type=int, default=10_000, help="independent paths (default: 10000)")
    parser.add_argument("--days", type=float, default=365.0, help="simulation horizon in days (default: 365)")
    parser.add_argument("--steps", type=int, default=365, help="price updates per path (default: 365)")
    parser.add_argument("--volatility", type=float, default=0.8,
                        help="annualized volatility as a fraction (default: 0.8)")
    parser.add_argument("--drift", type=float, default=0.0,
                        help="annualized expected price return as a fraction (default: 0)")
    parser.add_argument("--seed", type=int, default=42, help="random seed (default: 42)")
    args = parser.parse_args(argv)
    try:
        config = SimulationConfig(args.paths, args.days, args.steps, args.volatility,
                                  args.drift, args.seed)
        result = simulate(config)
    except ValueError as error:
        parser.error(str(error))
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
