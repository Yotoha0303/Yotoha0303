#!/usr/bin/env python3
"""Fuzz Uniswap v3 and Curve pool equations under DeFi liquidity stress.

The simulator is deliberately self contained and deterministic.  It exercises
the Uniswap v3 virtual-reserve equations and Curve's two-coin StableSwap
invariant over independent swap paths.  Each path can include a sudden
liquidity drain, a large flash-loan style swap, an oracle desynchronisation,
or all three.  Results are loss percentages relative to the initial pool
value.  VaR is the positive loss at the configured confidence percentile.

Run ``python3 scripts/defi_liquidity_stress_fuzzer.py --output benchmark.json``
to export the default 20,000-path benchmark as JSON.
"""

import argparse
import json
import math
import random
import sys
from dataclasses import asdict, dataclass
from pathlib import Path


EPSILON = 1e-12
DEFAULT_PROTOCOLS = ("uniswap_v3", "curve")
DEFAULT_SCENARIOS = ("liquidity_drain", "flash_loan", "oracle_desync", "combined")


def _number(value, name, *, positive=False, nonnegative=False, maximum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    try:
        result = float(value)
    except (OverflowError, ValueError) as error:
        raise ValueError(f"{name} must be a finite number") from error
    if not math.isfinite(result):
        raise ValueError(f"{name} must be a finite number")
    if positive and result <= 0 or nonnegative and result < 0:
        raise ValueError(f"{name} is outside its permitted range")
    if maximum is not None and result > maximum:
        raise ValueError(f"{name} is outside its permitted range")
    return result


def _positive_int(value, name, minimum=1):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer of at least {minimum}")
    return value


def _quantile(values, probability):
    if not values:
        raise ValueError("quantile requires at least one value")
    if not 0 <= probability <= 1:
        raise ValueError("probability must be between 0 and 1")
    ordered = sorted(values)
    position = probability * (len(ordered) - 1)
    lower = int(position)
    fraction = position - lower
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + fraction * (ordered[upper] - ordered[lower])


def _finite_result(value, name):
    if not math.isfinite(value):
        raise ValueError(f"{name} became non-finite")
    return value


@dataclass
class SwapResult:
    amount_in: float
    amount_out: float
    fee_paid: float
    price_impact: float
    invariant_before: float
    invariant_after: float


@dataclass
class UniswapV3Pool:
    """A bounded Uniswap v3 liquidity range using its virtual-reserve math.

    ``sqrt_price`` is token1 per token0 in square-root form and ``liquidity``
    is active liquidity L. Swaps cannot cross the configured range; no other
    tick has liquidity. Fees are excluded from active liquidity accounting.
    """

    sqrt_price: float = 1.0
    liquidity: float = 1_000_000.0
    fee: float = 0.003
    sqrt_price_lower: float = 0.5
    sqrt_price_upper: float = 2.0

    def __post_init__(self):
        _number(self.sqrt_price, "sqrt_price", positive=True)
        _number(self.liquidity, "liquidity", positive=True)
        _number(self.fee, "fee", nonnegative=True, maximum=0.999999)
        _number(self.sqrt_price_lower, "sqrt_price_lower", positive=True)
        _number(self.sqrt_price_upper, "sqrt_price_upper", positive=True)
        if not self.sqrt_price_lower < self.sqrt_price < self.sqrt_price_upper:
            raise ValueError("sqrt_price must be inside the liquidity range")

    def clone(self):
        return UniswapV3Pool(self.sqrt_price, self.liquidity, self.fee,
                             self.sqrt_price_lower, self.sqrt_price_upper)

    @property
    def price(self):
        return self.sqrt_price * self.sqrt_price

    def reserves(self):
        return (self.liquidity * (1.0 / self.sqrt_price - 1.0 / self.sqrt_price_upper),
                self.liquidity * (self.sqrt_price - self.sqrt_price_lower))

    def invariant(self):
        return self.liquidity * self.liquidity

    def drain_liquidity(self, fraction):
        fraction = _number(fraction, "fraction", nonnegative=True, maximum=1.0)
        if fraction == 1:
            raise ValueError("a complete liquidity drain leaves no active pool")
        self.liquidity *= 1.0 - fraction

    def quote_swap(self, amount_in, zero_for_one=True):
        amount_in = _number(amount_in, "amount_in", positive=True)
        if not isinstance(zero_for_one, bool):
            raise ValueError("zero_for_one must be a boolean")
        effective = amount_in * (1.0 - self.fee)
        old_sqrt = self.sqrt_price
        before = self.invariant()
        if zero_for_one:
            next_sqrt = self.liquidity * old_sqrt / (self.liquidity + effective * old_sqrt)
            if next_sqrt <= self.sqrt_price_lower:
                raise ValueError("swap crosses the lower liquidity bound")
            amount_out = self.liquidity * (old_sqrt - next_sqrt)
            marginal = old_sqrt * old_sqrt
        else:
            next_sqrt = old_sqrt + effective / self.liquidity
            if next_sqrt >= self.sqrt_price_upper:
                raise ValueError("swap crosses the upper liquidity bound")
            amount_out = self.liquidity * (1.0 / old_sqrt - 1.0 / next_sqrt)
            marginal = old_sqrt * old_sqrt
        _finite_result(next_sqrt, "sqrt price")
        _finite_result(amount_out, "swap output")
        if amount_out <= 0:
            raise ValueError("swap output is zero")
        average = amount_out / amount_in if zero_for_one else amount_in / amount_out
        impact = max(0.0, 1.0 - average / marginal) if zero_for_one else max(0.0, 1.0 - marginal / average)
        return SwapResult(amount_in, amount_out, amount_in - effective, impact, before, before)

    def swap(self, amount_in, zero_for_one=True):
        result = self.quote_swap(amount_in, zero_for_one)
        effective = amount_in - result.fee_paid
        if zero_for_one:
            self.sqrt_price = self.liquidity * self.sqrt_price / (
                self.liquidity + effective * self.sqrt_price)
        else:
            self.sqrt_price += effective / self.liquidity
        result.invariant_after = self.invariant()
        return result


def curve_invariant(balances, amplification):
    """Return Curve's StableSwap invariant D for two or more balances."""
    if not isinstance(balances, (list, tuple)) or len(balances) < 2:
        raise ValueError("balances must contain at least two tokens")
    values = [_number(value, "balance", positive=True) for value in balances]
    amplification = _number(amplification, "amplification", positive=True)
    n = len(values)
    total = math.fsum(values)
    d = total
    ann = amplification * n
    for _ in range(255):
        d_product = d
        for value in values:
            d_product = d_product * d / (value * n)
        previous = d
        d = (ann * total + d_product * n) * d / ((ann - 1.0) * d + (n + 1.0) * d_product)
        if abs(d - previous) <= max(EPSILON, abs(d) * 1e-12):
            return d
    raise ValueError("Curve invariant did not converge")


def curve_y(balances, amplification, token_in, token_out, new_input_balance):
    """Solve the post-swap output balance while preserving Curve's D."""
    if token_in == token_out or not 0 <= token_in < len(balances) or not 0 <= token_out < len(balances):
        raise ValueError("token indices must be distinct and in range")
    values = [_number(value, "balance", positive=True) for value in balances]
    x = _number(new_input_balance, "new_input_balance", positive=True)
    amplification = _number(amplification, "amplification", positive=True)
    n = len(values)
    d = curve_invariant(values, amplification)
    ann = amplification * n
    c = d
    s = 0.0
    for index, value in enumerate(values):
        if index == token_in:
            value = x
        elif index == token_out:
            continue
        s += value
        c = c * d / (value * n)
    c = c * d / (ann * n)
    b = s + d / ann
    y = d
    for _ in range(255):
        previous = y
        y = (y * y + c) / (2.0 * y + b - d)
        if abs(y - previous) <= max(EPSILON, abs(y) * 1e-12):
            return y
    raise ValueError("Curve output balance did not converge")


@dataclass
class CurvePool:
    """Two-coin Curve StableSwap pool with the canonical D and y equations."""

    balances: list
    amplification: float = 100.0
    fee: float = 0.0004

    def __post_init__(self):
        if not isinstance(self.balances, list) or len(self.balances) != 2:
            raise ValueError("CurvePool currently supports exactly two balances")
        self.balances = [_number(value, "balance", positive=True) for value in self.balances]
        _number(self.amplification, "amplification", positive=True)
        _number(self.fee, "fee", nonnegative=True, maximum=0.999999)

    def clone(self):
        return CurvePool(list(self.balances), self.amplification, self.fee)

    def invariant(self):
        return curve_invariant(self.balances, self.amplification)

    @property
    def price(self):
        return self.balances[1] / self.balances[0]

    def drain_liquidity(self, fraction):
        fraction = _number(fraction, "fraction", nonnegative=True, maximum=1.0)
        if fraction == 1:
            raise ValueError("a complete liquidity drain leaves no pool")
        self.balances = [balance * (1.0 - fraction) for balance in self.balances]

    def quote_swap(self, amount_in, token_in=0):
        amount_in = _number(amount_in, "amount_in", positive=True)
        if isinstance(token_in, bool) or token_in not in (0, 1):
            raise ValueError("token_in must be 0 or 1")
        token_out = 1 - token_in
        effective = amount_in * (1.0 - self.fee)
        before = self.invariant()
        new_input = self.balances[token_in] + effective
        output_balance = curve_y(self.balances, self.amplification, token_in, token_out,
                                 new_input)
        amount_out = self.balances[token_out] - output_balance
        if not math.isfinite(amount_out) or amount_out <= 0 or amount_out >= self.balances[token_out]:
            raise ValueError("swap output is outside pool liquidity")
        marginal = self.price if token_in == 0 else 1.0 / self.price
        average = amount_out / amount_in
        impact = max(0.0, 1.0 - average / marginal)
        return SwapResult(amount_in, amount_out, amount_in - effective, impact, before, before)

    def swap(self, amount_in, token_in=0):
        result = self.quote_swap(amount_in, token_in)
        token_out = 1 - token_in
        effective = amount_in - result.fee_paid
        proposed = list(self.balances)
        proposed[token_in] += effective
        output_balance = self.balances[token_out] - result.amount_out
        proposed[token_out] = output_balance
        self.balances = proposed
        result.invariant_after = self.invariant()
        return result


@dataclass(frozen=True)
class StressConfig:
    paths: int = 20_000
    steps: int = 12
    seed: int = 42
    confidence: float = 0.99
    initial_liquidity: float = 1_000_000.0
    swap_fraction: float = 0.01
    max_drain_fraction: float = 0.80
    max_flash_loan_fraction: float = 8.0
    max_oracle_desync: float = 0.30

    def __post_init__(self):
        _positive_int(self.paths, "paths")
        _positive_int(self.steps, "steps")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int):
            raise ValueError("seed must be an integer")
        _number(self.confidence, "confidence", positive=True, maximum=0.999999)
        _number(self.initial_liquidity, "initial_liquidity", positive=True)
        _number(self.swap_fraction, "swap_fraction", positive=True, maximum=0.5)
        _number(self.max_drain_fraction, "max_drain_fraction", positive=True, maximum=0.999)
        if self.max_drain_fraction < 0.20:
            raise ValueError("max_drain_fraction must be at least 0.20")
        _number(self.max_flash_loan_fraction, "max_flash_loan_fraction", positive=True, maximum=100.0)
        if self.max_flash_loan_fraction < 2.0:
            raise ValueError("max_flash_loan_fraction must be at least 2")
        _number(self.max_oracle_desync, "max_oracle_desync", positive=True, maximum=0.99)


# This alias makes the intent discoverable to callers that use the script's
# filename when naming their configuration object.
FuzzerConfig = StressConfig


def _pool_value(pool, true_price=1.0):
    if isinstance(pool, UniswapV3Pool):
        token0, token1 = pool.reserves()
    else:
        token0, token1 = pool.balances
    return token0 * true_price + token1


def _new_pool(protocol, config):
    if protocol == "uniswap_v3":
        return UniswapV3Pool(1.0, config.initial_liquidity)
    if protocol == "curve":
        return CurvePool([config.initial_liquidity, config.initial_liquidity])
    raise ValueError(f"unsupported protocol: {protocol}")


def _scenario_event(scenario, pool, config, rng):
    flash = desync = 0.0
    if scenario in ("liquidity_drain", "combined"):
        pool.drain_liquidity(rng.uniform(0.20, config.max_drain_fraction))
    if scenario in ("flash_loan", "combined"):
        flash = rng.uniform(2.0, config.max_flash_loan_fraction)
    if scenario in ("oracle_desync", "combined"):
        desync = rng.uniform(-config.max_oracle_desync, config.max_oracle_desync)
    return flash, desync


def _run_path(protocol, scenario, config, rng):
    pool = _new_pool(protocol, config)
    initial_value = _pool_value(pool)
    values = [initial_value]
    oracle_price = 1.0
    failed_swaps = 0
    executed_swaps = 0
    worst_shortfall = 0.0
    event_step = rng.randrange(config.steps)
    victim_notional = initial_value * config.swap_fraction
    for step in range(config.steps):
        flash_fraction = 0.0
        if step == event_step:
            flash_fraction, desync = _scenario_event(scenario, pool, config, rng)
            oracle_price *= 1.0 + desync
        token_direction = rng.randrange(2)
        attacker_output = None
        if flash_fraction:
            try:
                attacker_amount = victim_notional * flash_fraction
                if isinstance(pool, UniswapV3Pool):
                    attacker_output = pool.swap(attacker_amount, token_direction == 0).amount_out
                else:
                    attacker_output = pool.swap(attacker_amount, token_direction).amount_out
                executed_swaps += 1
            except ValueError:
                failed_swaps += 1
        try:
            if isinstance(pool, UniswapV3Pool):
                result = pool.swap(victim_notional, token_direction == 0)
            else:
                result = pool.swap(victim_notional, token_direction)
            executed_swaps += 1
            oracle_rate = oracle_price if token_direction == 0 else 1.0 / oracle_price
            shortfall = max(0.0, 1.0 - result.amount_out / (victim_notional * oracle_rate))
            worst_shortfall = max(worst_shortfall, shortfall)
        except ValueError:
            failed_swaps += 1
            worst_shortfall = 1.0
        if attacker_output is not None:
            try:
                if isinstance(pool, UniswapV3Pool):
                    pool.swap(attacker_output, token_direction != 0)
                else:
                    pool.swap(attacker_output, 1 - token_direction)
                executed_swaps += 1
            except ValueError:
                failed_swaps += 1
        values.append(_pool_value(pool))
    peak = values[0]
    max_drawdown = 0.0
    for value in values:
        peak = max(peak, value)
        max_drawdown = max(max_drawdown, (peak - value) / peak)
    return {
        "loss_pct": worst_shortfall * 100.0,
        "max_drawdown_pct": max_drawdown * 100.0,
        "failed_swaps": failed_swaps,
        "executed_swaps": executed_swaps,
    }


def _matrix(statistics, protocols, scenarios, field):
    return {
        "protocols": list(protocols),
        "scenarios": list(scenarios),
        "values": [[statistics[(protocol, scenario)][field]
                    for scenario in scenarios] for protocol in protocols],
    }


def simulate(config=None, *, protocols=DEFAULT_PROTOCOLS, scenarios=DEFAULT_SCENARIOS):
    """Run the stress fuzzer and return JSON-serializable benchmark statistics."""
    if config is None:
        config = StressConfig()
    if not isinstance(config, StressConfig):
        raise TypeError("config must be a StressConfig")
    protocols = tuple(protocols)
    scenarios = tuple(scenarios)
    if not protocols or not scenarios:
        raise ValueError("protocols and scenarios must not be empty")
    if any(protocol not in DEFAULT_PROTOCOLS for protocol in protocols):
        raise ValueError("unsupported protocol")
    if any(scenario not in DEFAULT_SCENARIOS for scenario in scenarios):
        raise ValueError("unsupported scenario")
    if len(set(protocols)) != len(protocols) or len(set(scenarios)) != len(scenarios):
        raise ValueError("protocols and scenarios must be unique")
    cells = len(protocols) * len(scenarios)
    if config.paths < cells:
        raise ValueError("paths must cover every protocol/scenario cell")

    aggregate = {}
    total_paths = 0
    rng = random.Random(config.seed)
    for cell_index, (protocol, scenario) in enumerate(
            (protocol, scenario) for protocol in protocols for scenario in scenarios):
            cell_paths = config.paths // cells + (cell_index < config.paths % cells)
            path_results = [_run_path(protocol, scenario, config, rng) for _ in range(cell_paths)]
            losses = [row["loss_pct"] for row in path_results]
            drawdowns = [row["max_drawdown_pct"] for row in path_results]
            aggregate[(protocol, scenario)] = {
                "path_count": cell_paths,
                "var_99_pct": _quantile(losses, config.confidence),
                "max_drawdown_pct": max(drawdowns),
                "drawdown_var_99_pct": _quantile(drawdowns, config.confidence),
                "mean_loss_pct": math.fsum(losses) / cell_paths,
                "mean_max_drawdown_pct": math.fsum(drawdowns) / cell_paths,
                "failed_swaps": sum(row["failed_swaps"] for row in path_results),
                "executed_swaps": sum(row["executed_swaps"] for row in path_results),
            }
            total_paths += cell_paths

    var_matrix = _matrix(aggregate, protocols, scenarios, "var_99_pct")
    drawdown_matrix = _matrix(aggregate, protocols, scenarios, "max_drawdown_pct")
    drawdown_var_matrix = _matrix(aggregate, protocols, scenarios, "drawdown_var_99_pct")
    return {
        "schema_version": "1.0.0",
        "model": "Uniswap v3 virtual reserves and Curve StableSwap D/y equations",
        "parameters": {**asdict(config), "protocols": list(protocols), "scenarios": list(scenarios)},
        "path_count": total_paths,
        "var_99_pct_matrix": var_matrix,
        "max_drawdown_pct_matrix": drawdown_matrix,
        "drawdown_var_99_pct_matrix": drawdown_var_matrix,
        "statistics": {f"{protocol}:{scenario}": values for (protocol, scenario), values in aggregate.items()},
    }


run_fuzzer = simulate
benchmark_statistics = simulate


def export_json(result, output):
    """Write benchmark ``result`` to a UTF-8 JSON file and return its path."""
    if not isinstance(result, dict):
        raise TypeError("result must be a dictionary")
    path = Path(output)
    path.write_text(json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--paths", type=int, default=20_000)
    parser.add_argument("--steps", type=int, default=12)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--output", type=Path, help="write benchmark JSON to this file")
    args = parser.parse_args(argv)
    try:
        result = simulate(StressConfig(paths=args.paths, steps=args.steps, seed=args.seed))
        if args.output:
            export_json(result, args.output)
        print(json.dumps(result, indent=2, allow_nan=False))
    except (OSError, TypeError, ValueError) as error:
        print(f"fuzzer input error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
