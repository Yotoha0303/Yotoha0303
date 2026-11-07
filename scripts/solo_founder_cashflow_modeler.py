#!/usr/bin/env python3
"""Project a solo founder's cashflow and first-shortfall survival using only stdlib.

Run a reproducible 10,000-path, twelve-period projection::

    python3 scripts/solo_founder_cashflow_modeler.py --output financial_health.md
    python3 scripts/solo_founder_cashflow_modeler.py --initial-cash 15000 \
        --living-expenses 3000 --hosting 300 --proxies 200 --days 180

All periods contain exactly 30 days, not calendar months. Monetary inputs are
CNY unless explicitly marked USD, rounded half up to fen. Fixed burn is spread
across days with cumulative integer apportionment, so every 30 days costs exactly
the specified amount. Each freelance opportunity independently wins with a
configured probability, pays a uniform fee (default ¥1,500–¥8,000), and settles
on a uniform day in its period plus a payment delay. API volume is rounded
lognormal query volume per full period, using an arithmetic mean and coefficient
of variation; zero CV gives deterministic volume. Queries are billed at $0.05
by default, converted together at a fixed CNY/USD rate, and paid at period end
plus a delay. Unsettled receipts beyond the horizon do not enter cashflow.

Salary milestones are conditional first-payday opportunities: if no earlier
milestone succeeded, a Bernoulli trial may start a recurring 30-day salary on
that day. Later milestones are ignored after success; they are not bonuses or
raises. All defaults are illustrative assumptions, not measured forecasts.

Receipts post before that day's burn. A negative daily closing balance is the
first shortfall; exactly zero is solvent. Survival never recovers after failure.
Cash projections continue through deficits to show financing needs, so positive
terminal cash alone does not imply survival. Funded runway is the number of
days before first shortfall, capped (right-censored) at the simulation horizon.
Tracks and periods are independent except for the persistent salary state.
Taxes, transaction fees, churn, FX volatility, credit, and work-capacity limits
are excluded; enter net receipts or include extra costs in burn as appropriate.

The Python API is SimulationConfig -> simulate -> generate_markdown_report;
simulate_path exposes an individual daily ledger for inspecting assumptions.
--config accepts a JSON object with the same fields as SimulationConfig (nested
burn, freelance, api objects and a salary_milestones list). CLI flags override
JSON fields. For example: {"salary_milestones": [], "freelance":
{"win_probability": 0}, "api": {"expected_queries_per_30_days": 0}} disables
all income. --salary-milestone DAY:CNY:PROBABILITY may be repeated, in day order.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field, fields
from decimal import Decimal, ROUND_HALF_UP
import json
import math
from pathlib import Path
import random


DAYS_PER_PERIOD = 30
TRACKS = ("freelance", "api", "salary")


def _number(value, name, *, minimum=0, maximum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite or value < minimum or maximum is not None and value > maximum:
        raise ValueError(f"{name} must be finite and in {minimum}..{maximum or 'infinity'}")
    return value


def _integer(value, name, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def _money_fen(value, name="amount"):
    _number(value, name)
    return int((Decimal(str(value)) * 100).to_integral_value(rounding=ROUND_HALF_UP))


@dataclass(frozen=True)
class BurnRate:
    living_expenses_cny: float = 3000.0
    hosting_cny: float = 300.0
    proxies_cny: float = 200.0

    def __post_init__(self):
        for item in fields(self):
            _money_fen(getattr(self, item.name), item.name)

    @property
    def total_fen(self):
        return sum(_money_fen(getattr(self, item.name)) for item in fields(self))

    @property
    def total_cny(self):
        return self.total_fen / 100


@dataclass(frozen=True)
class FreelanceTrack:
    opportunities_per_30_days: int = 3
    win_probability: float = 0.35
    min_fee_cny: float = 1500.0
    max_fee_cny: float = 8000.0
    payment_delay_days: int = 7

    def __post_init__(self):
        _integer(self.opportunities_per_30_days, "opportunities_per_30_days")
        _number(self.win_probability, "win_probability", maximum=1)
        _money_fen(self.min_fee_cny, "min_fee_cny")
        _money_fen(self.max_fee_cny, "max_fee_cny")
        if self.min_fee_cny > self.max_fee_cny:
            raise ValueError("min_fee_cny must not exceed max_fee_cny")
        _integer(self.payment_delay_days, "payment_delay_days")


@dataclass(frozen=True)
class MeteringTrack:
    expected_queries_per_30_days: float = 1000.0
    volume_cv: float = 0.6
    usd_per_query: float = 0.05
    usd_to_cny: float = 7.2
    payout_delay_days: int = 0

    def __post_init__(self):
        for name in ("expected_queries_per_30_days", "volume_cv", "usd_per_query", "usd_to_cny"):
            _number(getattr(self, name), name)
        if self.usd_to_cny == 0:
            raise ValueError("usd_to_cny must be positive")
        _integer(self.payout_delay_days, "payout_delay_days")


@dataclass(frozen=True)
class SalaryMilestone:
    day: int
    monthly_salary_cny: float
    probability: float

    def __post_init__(self):
        _integer(self.day, "salary milestone day", 1)
        _money_fen(self.monthly_salary_cny, "monthly_salary_cny")
        _number(self.probability, "salary milestone probability", maximum=1)


@dataclass(frozen=True)
class SimulationConfig:
    initial_cash_cny: float = 30000.0
    horizon_days: int = 360
    paths: int = 10_000
    seed: int = 42
    burn: BurnRate = field(default_factory=BurnRate)
    freelance: FreelanceTrack = field(default_factory=FreelanceTrack)
    api: MeteringTrack = field(default_factory=MeteringTrack)
    salary_milestones: tuple[SalaryMilestone, ...] = field(default_factory=lambda: (
        SalaryMilestone(90, 15000.0, 0.25), SalaryMilestone(150, 18000.0, 0.35),
    ))

    def __post_init__(self):
        _money_fen(self.initial_cash_cny, "initial_cash_cny")
        _integer(self.horizon_days, "horizon_days", 1)
        _integer(self.paths, "paths", 1)
        if type(self.seed) is not int:
            raise ValueError("seed must be an integer")
        for name, kind in (("burn", BurnRate), ("freelance", FreelanceTrack), ("api", MeteringTrack)):
            if not isinstance(getattr(self, name), kind):
                raise ValueError(f"{name} must be a {kind.__name__}")
        if not isinstance(self.salary_milestones, (tuple, list)) or not all(
                isinstance(item, SalaryMilestone) for item in self.salary_milestones):
            raise ValueError("salary_milestones must contain SalaryMilestone objects")
        milestones = tuple(self.salary_milestones)
        if any(a.day >= b.day for a, b in zip(milestones, milestones[1:])):
            raise ValueError("salary milestones must have strictly increasing days")
        object.__setattr__(self, "salary_milestones", milestones)


@dataclass(frozen=True)
class Receipt:
    day: int
    track: str
    amount_fen: int
    units: int = 1


@dataclass(frozen=True)
class PathProjection:
    """daily_cash_fen includes day zero; receipts include only settled inflows."""

    daily_cash_fen: tuple[int, ...]
    receipts: tuple[Receipt, ...]
    first_shortfall_day: int | None

    @property
    def funded_days(self):
        return (self.first_shortfall_day - 1 if self.first_shortfall_day is not None
                else len(self.daily_cash_fen) - 1)


def _daily_burn(config):
    total = config.burn.total_fen
    return tuple(day * total // DAYS_PER_PERIOD - (day - 1) * total // DAYS_PER_PERIOD
                 for day in range(1, config.horizon_days + 1))


def _sample_receipts(config, rng):
    receipts = []
    gig, api = config.freelance, config.api
    minimum, maximum = _money_fen(gig.min_fee_cny), _money_fen(gig.max_fee_cny)
    # log(1 + CV**2), avoiding overflow when CV is large.
    sigma = math.sqrt(2 * math.log(math.hypot(1, api.volume_cv)))
    for start in range(1, config.horizon_days + 1, DAYS_PER_PERIOD):
        if gig.win_probability:
            for _ in range(gig.opportunities_per_30_days):
                if rng.random() < gig.win_probability:
                    day = start + rng.randrange(DAYS_PER_PERIOD) + gig.payment_delay_days
                    amount = rng.randint(minimum, maximum)
                    if day <= config.horizon_days:
                        receipts.append(Receipt(day, "freelance", amount))
        payday = start + DAYS_PER_PERIOD - 1 + api.payout_delay_days
        if payday <= config.horizon_days and api.expected_queries_per_30_days:
            volume = api.expected_queries_per_30_days
            if sigma:
                volume *= rng.lognormvariate(-sigma * sigma / 2, sigma)
            if not math.isfinite(volume):
                raise ValueError("sampled API volume exceeds the finite numeric range")
            queries = int(volume + 0.5)
            # Round the entire USD settlement after conversion, not each query.
            amount = int((Decimal(queries) * Decimal(str(api.usd_per_query))
                          * Decimal(str(api.usd_to_cny)) * 100).to_integral_value(
                              rounding=ROUND_HALF_UP))
            receipts.append(Receipt(payday, "api", amount, queries))
    for milestone in config.salary_milestones:
        if milestone.day > config.horizon_days:
            break
        if rng.random() < milestone.probability:
            for day in range(milestone.day, config.horizon_days + 1, DAYS_PER_PERIOD):
                receipts.append(Receipt(day, "salary", _money_fen(milestone.monthly_salary_cny)))
            break
    return tuple(sorted(receipts, key=lambda receipt: (receipt.day, receipt.track)))


def _project_path(config, rng, burn):
    receipts = _sample_receipts(config, rng)
    inflows = [0] * (config.horizon_days + 1)
    for receipt in receipts:
        inflows[receipt.day] += receipt.amount_fen
    cash = _money_fen(config.initial_cash_cny)
    balances, first_shortfall = [cash], None
    for day, expense in enumerate(burn, 1):
        cash += inflows[day] - expense
        balances.append(cash)
        if cash < 0 and first_shortfall is None:
            first_shortfall = day
    return PathProjection(tuple(balances), receipts, first_shortfall)


def simulate_path(config=SimulationConfig()):
    """Return one reproducible daily ledger, using config.seed and ignoring paths."""
    if not isinstance(config, SimulationConfig):
        raise TypeError("config must be a SimulationConfig")
    return _project_path(config, random.Random(config.seed), _daily_burn(config))


def _distribution(values, divisor=1):
    ordered = sorted(values)

    def quantile(probability):
        position = probability * (len(ordered) - 1)
        lower = int(position)
        fraction = position - lower
        return (ordered[lower] + fraction * (
            ordered[min(lower + 1, len(ordered) - 1)] - ordered[lower])) / divisor

    try:
        result = {"min": ordered[0] / divisor, "mean": sum(ordered) / (len(ordered) * divisor),
                  "p10": quantile(0.1), "p50": quantile(0.5), "p90": quantile(0.9),
                  "max": ordered[-1] / divisor}
    except OverflowError as error:
        raise ValueError("projected cash exceeds the finite numeric range") from error
    if not all(math.isfinite(value) for value in result.values()):
        raise ValueError("projected cash exceeds the finite numeric range")
    return result


def _wilson_interval(successes, trials):
    """95% Wilson binomial interval, including all-success/all-failure samples."""
    p, z = successes / trials, 1.959963984540054
    denominator = 1 + z * z / trials
    center = (p + z * z / (2 * trials)) / denominator
    radius = z * math.sqrt(p * (1 - p) / trials + z * z / (4 * trials * trials)) / denominator
    # Floating-point cancellation can otherwise exclude p=0 or p=1 by an ulp.
    return {"lower": 0.0 if successes == 0 else max(0.0, center - radius),
            "upper": 1.0 if successes == trials else min(1.0, center + radius)}


def simulate(config=SimulationConfig()):
    """Return JSON-compatible cash distributions and first-passage survival counts.

    Cash quantiles use linear interpolation at p*(n-1); probabilities are in
    [0, 1]. Inflow means and cash projections include all paths, including those
    that previously failed. Horizon-capped runway is not an uncensored lifespan.
    """
    if not isinstance(config, SimulationConfig):
        raise TypeError("config must be a SimulationConfig")
    rng, burn = random.Random(config.seed), _daily_burn(config)
    checkpoints = list(range(DAYS_PER_PERIOD, config.horizon_days, DAYS_PER_PERIOD))
    checkpoints.append(config.horizon_days)
    balances = [[] for _ in checkpoints]
    receipts = [{track: 0 for track in TRACKS} for _ in checkpoints]
    failures = [0] * (config.horizon_days + 1)
    runways = []
    for _ in range(config.paths):
        path = _project_path(config, rng, burn)
        runways.append(path.funded_days)
        if path.first_shortfall_day is not None:
            failures[path.first_shortfall_day] += 1
        for samples, day in zip(balances, checkpoints):
            samples.append(path.daily_cash_fen[day])
        for receipt in path.receipts:
            receipts[(receipt.day - 1) // DAYS_PER_PERIOD][receipt.track] += receipt.amount_fen
    surviving, curve = config.paths, []
    for day, count in enumerate(failures):
        surviving -= count
        curve.append({"day": day, "surviving_paths": surviving,
                      "survival_probability": surviving / config.paths})
    monthly = []
    previous = 0
    for day, samples, inflows in zip(checkpoints, balances, receipts):
        monthly.append({
            "day": day, "days_in_period": day - previous,
            "fixed_burn_cny": sum(burn[previous:day]) / 100,
            "mean_receipts_cny": {track: amount / (config.paths * 100)
                                  for track, amount in inflows.items()},
            "cash_cny": _distribution(samples, 100),
            **curve[day],
            "survival_ci95": _wilson_interval(curve[day]["surviving_paths"], config.paths),
        })
        previous = day
    return {
        "currency": "CNY", "parameters": asdict(config),
        "fixed_burn_per_30_days_cny": config.burn.total_cny,
        "no_income_runway_days": (_money_fen(config.initial_cash_cny) * DAYS_PER_PERIOD
                                  / config.burn.total_fen if config.burn.total_fen else None),
        "surviving_paths": surviving, "survival_probability": surviving / config.paths,
        "survival_ci95": monthly[-1]["survival_ci95"],
        "runway_days": _distribution(runways), "runway_censored_paths": surviving,
        "terminal_cash_cny": monthly[-1]["cash_cny"],
        "mean_total_receipts_cny": {
            track: sum(row[track] for row in receipts) / (config.paths * 100) for track in TRACKS},
        "monthly_projection": monthly, "survival_curve": curve,
    }


def generate_markdown_report(result):
    """Format a simulate() result as a standalone financial health report."""
    config = result["parameters"]
    def money(value):
        return f"¥{value:,.2f}"

    probability = result["survival_probability"]
    health = "Stable" if probability >= 0.9 else "Watch" if probability >= 0.5 else "Critical"
    baseline = result["no_income_runway_days"]
    baseline_text = f"{baseline:,.1f} days" if baseline is not None else "Unlimited (zero fixed burn)"
    ci = result["survival_ci95"]
    median = result["runway_days"]["p50"]
    median_text = (f">= {config['horizon_days']} days" if median == config["horizon_days"]
                   else f"{median:,.1f} days")
    lines = [
        "# Solo Founder Financial Health Report", "",
        f"**Health: {health}** — {config['horizon_days']}-day survival: **{probability:.1%}**.", "",
        f"Simulation: {config['paths']:,} Monte Carlo paths; seed {config['seed']}; currency CNY.", "",
        "## Cash and runway", "", "| Metric | Estimate |", "| --- | ---: |",
        f"| Starting cash | {money(_money_fen(config['initial_cash_cny']) / 100)} |",
        f"| Fixed burn / 30 days | {money(result['fixed_burn_per_30_days_cny'])} |",
        f"| No-income runway (continuous burn estimate) | {baseline_text} |",
        f"| Median funded runway (horizon-capped) | {median_text} |",
        f"| Paths surviving entire horizon | {result['surviving_paths']:,} / {config['paths']:,} |",
        f"| Survival 95% Monte Carlo interval | {ci['lower']:.2%}–{ci['upper']:.2%} |",
        f"| Terminal cash P10 / median / P90 | "
        + " / ".join(money(result["terminal_cash_cny"][key]) for key in ("p10", "p50", "p90")) + " |",
        "", "## Fixed expenses", "", "| Expense | CNY / 30 days |", "| --- | ---: |",
    ]
    for label, name in (("Living expenses", "living_expenses_cny"), ("Hosting", "hosting_cny"),
                        ("Proxies", "proxies_cny")):
        lines.append(f"| {label} | {money(_money_fen(config['burn'][name]) / 100)} |")
    gig, api = config["freelance"], config["api"]
    lines += [
        "", "## Income assumptions", "",
        f"- Freelance: {gig['opportunities_per_30_days']} opportunities / 30 days, "
        f"{gig['win_probability']:.1%} independent win chance; uniform "
        f"{money(_money_fen(gig['min_fee_cny']) / 100)}–{money(_money_fen(gig['max_fee_cny']) / 100)} "
        f"fees; uniform arrival day plus {gig['payment_delay_days']} days to payment.",
        f"- Metering API: ${api['usd_per_query']:g} / query × {api['usd_to_cny']:g} CNY/USD; "
        f"{api['expected_queries_per_30_days']:g} mean queries / 30 days, rounded lognormal volume "
        f"with CV {api['volume_cv']:g}; paid at period end + {api['payout_delay_days']} days.",
        "- Salary: milestones are conditional first-payday trials if no earlier offer succeeded; "
        "the first success pays that day and every 30 days thereafter.", "",
        "| First payday | Salary / 30 days | Conditional success chance |",
        "| ---: | ---: | ---: |",
    ]
    for milestone in config["salary_milestones"]:
        lines.append(f"| Day {milestone['day']} | "
                     f"{money(_money_fen(milestone['monthly_salary_cny']) / 100)} | {milestone['probability']:.1%} |")
    if not config["salary_milestones"]:
        lines.append("| — | Disabled | — |")
    lines += [
        "", "## Cashflow projection", "",
        "Means include every path. Cash after a shortfall is hypothetical and requires financing.", "",
        "| Day | Burn | Freelance mean | API mean | Salary mean | Cash mean | Cash P10 | Cash median | Cash P90 | Survival |",
        "| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in result["monthly_projection"]:
        values = [row["fixed_burn_cny"]] + [row["mean_receipts_cny"][track] for track in TRACKS]
        values += [row["cash_cny"][key] for key in ("mean", "p10", "p50", "p90")]
        lines.append(f"| {row['day']} | " + " | ".join(map(money, values))
                     + f" | {row['survival_probability']:.1%} |")
    lines += [
        "", "## Interpretation", "",
        "A path survives only while every daily closing balance is nonnegative. Receipts post "
        "before daily burn; a later payment never reverses an earlier survival failure. Each "
        "period is exactly 30 days; partial periods incur only their daily apportioned burn. "
        "API revenue arrives only on a completed period's payday; pending settlements are excluded.", "",
        f"Runway is capped at {config['horizon_days']} days; "
        f"{result['runway_censored_paths']:,} paths are right-censored and may last longer. "
        "Quantiles interpolate sample positions. The 95% Wilson interval measures simulation "
        "sampling error, not uncertainty in the assumptions.", "",
        "Health labels use horizon survival: Stable >= 90%, Watch >= 50%, Critical < 50%. "
        "Illustrative independent income assumptions exclude taxes, fees, churn, FX volatility, "
        "credit, and work-capacity limits. Adjust amounts to net receipts and actual expenses.", "",
    ]
    return "\n".join(lines)


def config_from_dict(data):
    """Load strict nested JSON configuration; omitted fields retain defaults."""
    if not isinstance(data, dict):
        raise ValueError("configuration must be a JSON object")
    values = dict(data)
    try:
        for name, kind in (("burn", BurnRate), ("freelance", FreelanceTrack), ("api", MeteringTrack)):
            if name in values:
                if not isinstance(values[name], dict):
                    raise ValueError(f"{name} must be a JSON object")
                values[name] = kind(**values[name])
        if "salary_milestones" in values:
            if not isinstance(values["salary_milestones"], (tuple, list)) or not all(
                    isinstance(item, dict) for item in values["salary_milestones"]):
                raise ValueError("salary_milestones must be a list of JSON objects")
            values["salary_milestones"] = tuple(SalaryMilestone(**item) for item in values["salary_milestones"])
        return SimulationConfig(**values)
    except TypeError as error:
        raise ValueError(f"invalid configuration fields: {error}") from error


def _milestone_argument(text):
    try:
        day, salary, probability = text.split(":")
        return asdict(SalaryMilestone(int(day), float(salary), float(probability)))
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"expected DAY:CNY:PROBABILITY: {error}") from error


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", type=Path, help="JSON configuration; flags override its fields")
    parser.add_argument("--output", type=Path, help="write UTF-8 Markdown here instead of stdout")
    options = {
        "initial_cash_cny": ("--initial-cash", float), "horizon_days": ("--days", int),
        "paths": ("--paths", int), "seed": ("--seed", int),
        "burn.living_expenses_cny": ("--living-expenses", float),
        "burn.hosting_cny": ("--hosting", float), "burn.proxies_cny": ("--proxies", float),
        "freelance.opportunities_per_30_days": ("--gig-opportunities", int),
        "freelance.win_probability": ("--gig-win-probability", float),
        "freelance.min_fee_cny": ("--gig-min", float), "freelance.max_fee_cny": ("--gig-max", float),
        "freelance.payment_delay_days": ("--gig-payment-delay", int),
        "api.expected_queries_per_30_days": ("--api-queries", float),
        "api.volume_cv": ("--api-volume-cv", float), "api.usd_per_query": ("--api-rate-usd", float),
        "api.usd_to_cny": ("--usd-to-cny", float), "api.payout_delay_days": ("--api-payout-delay", int),
    }
    for name, (flag, kind) in options.items():
        parser.add_argument(flag, dest=name, type=kind, help=f"override {name}")
    salary = parser.add_mutually_exclusive_group()
    salary.add_argument("--salary-milestone", action="append", type=_milestone_argument,
                        help="DAY:CNY:PROBABILITY; repeat in increasing day order")
    salary.add_argument("--no-salary", action="store_true", help="disable salary income")
    args = parser.parse_args(argv)
    try:
        raw = json.loads(args.config.read_text(encoding="utf-8")) if args.config else {}
        # Validate the input before merging overrides; unknown fields never vanish.
        data = asdict(config_from_dict(raw))
        for name in options:
            value = getattr(args, name)
            if value is not None:
                if "." in name:
                    section, key = name.split(".")
                    data[section][key] = value
                else:
                    data[name] = value
        if args.no_salary:
            data["salary_milestones"] = []
        elif args.salary_milestone is not None:
            data["salary_milestones"] = args.salary_milestone
        report = generate_markdown_report(simulate(config_from_dict(data)))
        if args.output:
            args.output.write_text(report, encoding="utf-8")
        else:
            print(report, end="")
    except (OSError, UnicodeError, ValueError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
