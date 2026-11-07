#!/usr/bin/env python3
"""Deterministic Double-100 product evaluation, inspired by commercial pragmatism.

This local rubric translates commercial pragmatism into explicit scoring rules.
Inputs describe evidence assessed by the caller. All eight criteria are required.
Ratings are 0..10:
0 = absent, 5 = partial/moderate, 10 = strongest evidence. For saturation only,
0 = uncrowded and 10 = crowded with interchangeable alternatives.

Example product (valid JSON, or equivalent YAML)::

    {
      "id": "invoice-helper", "name": "Invoice Helper",
      "description": "Resolve overdue invoices with immediate feedback.",
      "market": {
        "pain_urgency": 9, "quick_dopamine_consumption": 8,
        "competitive_saturation": 3, "willingness_to_pay": 9
      },
      "delivery": {
        "sdlc_compliance": 9, "airgap_security": 10, "mvp_days": 14,
        "unit_revenue": 10, "unit_cost": 2
      }
    }

Market & Demand (A, 100): urgency 35, quick dopamine/instant gratification 20,
inverse saturation 20, willingness to pay 25. Rating points = weight * rating/10.
Quick dopamine means fast, repeatable satisfaction from consuming the product.

Technical & Delivery (B, 100): SDLC 25, airgap security 25, MVP feasibility 25,
unit economics 25. SDLC ratings assess requirements, review, tests, releases and
rollback. Airgap ratings assess offline operation, egress controls, data and
secret isolation. MVP uses calendar days: <=14 earns 25; 14..28 declines linearly
to zero; >=28 earns zero. Margin points = 25 * max(0, (revenue-cost)/revenue);
zero revenue earns zero. Revenue and all variable delivery/support costs must
use the same currency, period and billing unit. Fixed costs are outside this
gross contribution margin model.

Each criterion rounds HALF_UP to two decimals using exact rational arithmetic;
dimensions sum the rounded criteria. PRIORITIZE requires both dimensions >=80,
VALIDATE both >=60, otherwise DEFER. SDLC or airgap ratings <8, MVP >14 days,
zero revenue, or non-positive margin also force DEFER. These explicit blockers
keep market enthusiasm from compensating for delivery constraints.

Ranking: descending min(A,B), then A+B, then A, then casefolded ID and exact ID.
Ranking is independent of input order. Reports include blockers separately.
No network, random state, clock, or ambient decimal context is used.

Files may contain a single product, a list, or {"products": [...]}. JSON needs
only Python's standard library; YAML uses optional PyYAML (pip install PyYAML)
with a safe loader. Duplicate keys and unknown fields are rejected in both.

    python scripts/commercial_viability_scoring_engine.py products.yaml --output report.md
    python scripts/commercial_viability_scoring_engine.py one.json two.yaml --format json
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from dataclasses import asdict, dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any, Iterable, Mapping


RUBRIC_VERSION = "1.0"
MARKET_WEIGHTS = (
    ("pain_urgency", "Pain urgency", 35),
    ("quick_dopamine_consumption", "Quick dopamine consumption", 20),
    ("competitive_saturation", "Competitive saturation (inverse)", 20),
    ("willingness_to_pay", "Willingness to pay", 25),
)
DELIVERY_RATINGS = (
    ("sdlc_compliance", "SDLC compliance", 25),
    ("airgap_security", "Airgap security", 25),
)


def _object(value: Any, required: set[str], optional: set[str], path: str) -> Mapping:
    if not isinstance(value, Mapping):
        raise ValueError(f"{path} must be an object")
    if any(not isinstance(key, str) for key in value):
        raise ValueError(f"{path} keys must be strings")
    missing = required - value.keys()
    unknown = value.keys() - required - optional
    if missing:
        raise ValueError(f"{path} missing fields: {', '.join(sorted(missing))}")
    if unknown:
        raise ValueError(f"{path} unknown fields: {', '.join(sorted(unknown))}")
    return value


def _text(value: Any, path: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise ValueError(f"{path} must be a {'possibly empty' if allow_empty else 'nonempty'} string")
    return value.strip()


def _number(value: Any, path: str, maximum: int | None = None) -> Fraction:
    # Strings and booleans are intentionally not coerced to numeric ratings.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{path} must be a finite number")
    try:
        number = Fraction(str(value))
    except (ValueError, OverflowError) as exc:
        raise ValueError(f"{path} must be a finite number") from exc
    if number < 0 or (maximum is not None and number > maximum):
        raise ValueError(f"{path} must be in 0..{maximum}" if maximum is not None
                         else f"{path} must be nonnegative")
    return number


def _validate_product(product: Any) -> dict[str, Any]:
    product = _object(product, {"id", "name", "market", "delivery"}, {"description"}, "product")
    market_keys = {key for key, _, _ in MARKET_WEIGHTS}
    delivery_keys = {key for key, _, _ in DELIVERY_RATINGS} | {"mvp_days", "unit_revenue", "unit_cost"}
    market = _object(product["market"], market_keys, set(), "market")
    delivery = _object(product["delivery"], delivery_keys, set(), "delivery")
    return {
        "id": _text(product["id"], "id"),
        "name": _text(product["name"], "name"),
        "description": _text(product.get("description", ""), "description", allow_empty=True),
        "market": {key: _number(market[key], f"market.{key}", 10) for key in sorted(market_keys)},
        "delivery": {key: _number(delivery[key], f"delivery.{key}",
                                  10 if key in {"sdlc_compliance", "airgap_security"} else None)
                     for key in sorted(delivery_keys)},
    }


def _rounded(value: Fraction) -> float:
    """Nonnegative rational to hundredths, with no binary/decimal tie ambiguity."""
    cents = (2 * value.numerator * 100 + value.denominator) // (2 * value.denominator)
    return cents / 100


@dataclass(frozen=True)
class CriterionScore:
    key: str
    label: str
    score: float
    max_points: int
    rationale: str


@dataclass(frozen=True)
class DimensionScore:
    name: str
    score: float
    criteria: tuple[CriterionScore, ...]
    max_points: int = 100


@dataclass(frozen=True)
class ProductEvaluation:
    product_id: str
    name: str
    description: str
    market: DimensionScore
    delivery: DimensionScore
    recommendation: str
    blockers: tuple[str, ...]

    @property
    def combined_score(self) -> float:
        return round(self.market.score + self.delivery.score, 2)

    @property
    def bottleneck_score(self) -> float:
        return min(self.market.score, self.delivery.score)

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "combined_score": self.combined_score,
                "bottleneck_score": self.bottleneck_score}


def _dimension(name: str, criteria: list[CriterionScore]) -> DimensionScore:
    total = sum((Fraction(str(item.score)) for item in criteria), Fraction(0))
    return DimensionScore(name, _rounded(total), tuple(criteria))


def _ranking_key(result: ProductEvaluation) -> tuple:
    return (-result.bottleneck_score, -result.combined_score, -result.market.score,
            result.product_id.casefold(), result.product_id)


class CommercialViabilityScoringEngine:
    """Stateless evaluator. Call score(mapping) or rank(iterable_of_mappings)."""

    def score(self, product: Mapping[str, Any]) -> ProductEvaluation:
        spec = _validate_product(product)
        market, delivery = spec["market"], spec["delivery"]
        market_criteria = []
        for key, label, weight in MARKET_WEIGHTS:
            rating = market[key]
            inverse = key == "competitive_saturation"
            ratio = (10 - rating if inverse else rating) / 10
            market_criteria.append(CriterionScore(
                key, label, _rounded(weight * ratio), weight,
                f"Rating {rating}/10; {weight} × " +
                ("(10 - rating)/10 (less saturation is better)." if inverse else "rating/10."),
            ))
        delivery_criteria = [
            CriterionScore(key, label, _rounded(weight * delivery[key] / 10), weight,
                           f"Rating {delivery[key]}/10; {weight} × rating/10.")
            for key, label, weight in DELIVERY_RATINGS
        ]
        days = delivery["mvp_days"]
        feasibility = max(Fraction(0), min(Fraction(1), (28 - days) / 14))
        revenue, cost = delivery["unit_revenue"], delivery["unit_cost"]
        margin = (revenue - cost) / revenue if revenue else Fraction(0)
        delivery_criteria.extend([
            CriterionScore("mvp_feasibility", "2-week MVP feasibility", _rounded(25 * feasibility), 25,
                           f"{days} calendar days; full points at <=14, linear decline to zero at 28."),
            CriterionScore("unit_economics_margin", "Unit economics margin", _rounded(25 * max(0, margin)), 25,
                           f"Revenue {revenue}, variable cost {cost}; 25 × max(0, (revenue-cost)/revenue); "
                           "zero revenue earns zero."),
        ])
        dimension_a = _dimension("A: Market & Demand", market_criteria)
        dimension_b = _dimension("B: Technical & Delivery", delivery_criteria)
        blockers = []
        for key, label, _ in DELIVERY_RATINGS:
            if delivery[key] < 8:
                blockers.append(f"{label} rating is below 8/10.")
        if days > 14:
            blockers.append("MVP estimate exceeds 14 calendar days.")
        if not revenue:
            blockers.append("Unit revenue is zero; paid demand and margin are unproven.")
        elif margin <= 0:
            blockers.append("Unit economics margin is non-positive.")
        weaker = min(dimension_a.score, dimension_b.score)
        recommendation = ("prioritize" if weaker >= 80 else "validate" if weaker >= 60 else "defer")
        if blockers:
            recommendation = "defer"
        return ProductEvaluation(spec["id"], spec["name"], spec["description"],
                                 dimension_a, dimension_b, recommendation, tuple(blockers))

    def rank(self, products: Iterable[Mapping[str, Any]]) -> list[ProductEvaluation]:
        results = [self.score(product) for product in products]
        seen = set()
        for result in results:
            identity = result.product_id.casefold()
            if identity in seen:
                raise ValueError(f"duplicate product id: {result.product_id}")
            seen.add(identity)
        return sorted(results, key=_ranking_key)


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key: {key}")
        result[key] = value
    return result


def _load_yaml(text: str) -> Any:
    try:
        import yaml
    except ImportError as exc:
        raise ValueError("YAML input requires PyYAML; install with: pip install PyYAML") from exc

    class UniqueSafeLoader(yaml.SafeLoader):
        pass

    def construct_mapping(loader, node):
        pairs = []
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node, deep=True)
            if not isinstance(key, str):
                raise ValueError("YAML object keys must be strings")
            pairs.append((key, loader.construct_object(value_node, deep=True)))
        return _unique_pairs(pairs)

    UniqueSafeLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, construct_mapping)
    try:
        return yaml.load(text, Loader=UniqueSafeLoader)
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid YAML: {exc}") from exc


def load_product_specs(path: str | Path) -> list[dict[str, Any]]:
    """Read and validate one UTF-8 .json/.yaml/.yml file; do not mutate its data."""
    path = Path(path)
    if path.suffix.lower() not in {".json", ".yaml", ".yml"}:
        raise ValueError(f"unsupported specification format: {path.suffix or '(none)'}")
    try:
        text = path.read_text(encoding="utf-8")
        data = json.loads(text, object_pairs_hook=_unique_pairs) if path.suffix.lower() == ".json" else _load_yaml(text)
        if isinstance(data, Mapping) and "products" in data:
            _object(data, {"products"}, set(), "document")
            products = data["products"]
        else:
            products = [data] if isinstance(data, Mapping) else data
        if not isinstance(products, list) or not products:
            raise ValueError("document must contain a product or a nonempty products list")
        for product in products:
            _validate_product(product)
        return products
    except (ValueError, UnicodeError) as exc:
        raise ValueError(f"{path}: {exc}") from exc


def _markdown(text: str) -> str:
    text = html.escape(text, quote=False)
    for character in "\\`*_{}[]()#+-.!|":
        text = text.replace(character, "\\" + character)
    return " ".join(text.split())


def generate_markdown_report(evaluations: Iterable[ProductEvaluation]) -> str:
    """Render a reproducible ranking and a full, explainable scoring breakdown."""
    results = sorted(evaluations, key=_ranking_key)
    lines = [
        "# Double-100 Commercial Viability Report", "", f"Rubric version: {RUBRIC_VERSION}", "",
        "A: Market & Demand /100. B: Technical & Delivery /100. Combined /200.",
        "Rank: weaker dimension, combined score, market score (descending), then product ID (ascending).", "",
        "Prioritize: both >=80. Validate: both >=60. Otherwise defer. Any delivery blocker forces defer.",
        "Ratings reflect caller-assessed evidence. Criterion scores round HALF_UP to two decimals before summing.", "",
        "| Rank | Product | ID | A /100 | B /100 | Combined /200 | Decision |",
        "| --- | --- | --- | ---: | ---: | ---: | --- |",
    ]
    for index, result in enumerate(results, 1):
        lines.append(f"| {index} | {_markdown(result.name)} | {_markdown(result.product_id)} | "
                     f"{result.market.score:.2f} | {result.delivery.score:.2f} | "
                     f"{result.combined_score:.2f} | {result.recommendation.upper()} |")
    if not results:
        lines.extend(["", "No products evaluated."])
    for result in results:
        lines.extend(["", f"## {_markdown(result.name)} ({_markdown(result.product_id)})", ""])
        if result.description:
            lines.extend([_markdown(result.description), ""])
        for dimension in (result.market, result.delivery):
            lines.extend([f"### {dimension.name} — {dimension.score:.2f}/100", "",
                          "| Criterion | Points | Calculation |", "| --- | ---: | --- |"])
            for criterion in dimension.criteria:
                lines.append(f"| {criterion.label} | {criterion.score:.2f}/{criterion.max_points} | "
                             f"{_markdown(criterion.rationale)} |")
            lines.append("")
        lines.append(f"Decision: **{result.recommendation.upper()}**.")
        if result.blockers:
            lines.extend(["", "Delivery blockers:", ""])
            lines.extend(f"- {_markdown(blocker)}" for blocker in result.blockers)
        else:
            lines.extend(["", "Delivery blockers: none."])
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("specs", nargs="+", type=Path, help="JSON/YAML product specification files")
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    parser.add_argument("--output", type=Path, help="write a UTF-8 report instead of stdout")
    args = parser.parse_args(argv)
    try:
        products = [product for path in args.specs for product in load_product_specs(path)]
        results = CommercialViabilityScoringEngine().rank(products)
        report = (generate_markdown_report(results) if args.format == "markdown" else
                  json.dumps({"rubric_version": RUBRIC_VERSION,
                              "products": [{"rank": rank, **result.to_dict()}
                                           for rank, result in enumerate(results, 1)]},
                             ensure_ascii=False, indent=2, allow_nan=False) + "\n")
        if args.output:
            args.output.write_text(report, encoding="utf-8")
        else:
            sys.stdout.write(report)
    except (OSError, ValueError) as exc:
        parser.exit(2, f"error: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
