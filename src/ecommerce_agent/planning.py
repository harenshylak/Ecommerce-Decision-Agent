"""Bounded approximate rollouts using public forecasts and declared assumptions.

No evaluator, private scenario, actual-arrival, hidden elasticity, or evaluation
seed dependencies are imported or accepted by this module.
"""

import math
from dataclasses import dataclass

from ecommerce_agent.config import ProjectConfig
from ecommerce_agent.contracts import CreatePurchaseOrderAction, SetPriceAction, StoreSnapshot
from ecommerce_agent.forecasting import Forecast
from ecommerce_agent.sampling import negative_binomial_quantile, uniform
from ecommerce_agent.validation import MILLI, validate_plan

PLANNER_VERSION = "candidate-rollouts-0.1.0"


@dataclass(frozen=True)
class Candidate:
    sku: str
    price_cents: int
    order_units: int
    actions: tuple


def inventory_position(snapshot, sku):
    return next(i.stock for i in snapshot.inventory if i.sku == sku) + sum(
        o.quantity for o in snapshot.open_purchase_orders if o.sku == sku and o.status == "pending"
    )


def order_up_to(snapshot, forecast: Forecast, config: ProjectConfig) -> int:
    product = next(p for p in snapshot.products if p.sku == forecast.sku)
    lead = next(
        s.lead_time_days for s in snapshot.suppliers if s.supplier_id == product.supplier_id
    )
    demand = sum(p.mean_units for p in forecast.points[: lead + 7])
    needed = max(0, demand - inventory_position(snapshot, product.sku))
    pack = config.operating.case_pack
    quantity = math.ceil(needed / pack) * pack
    return (
        min(
            config.operating.maximum_order_units,
            max(config.operating.minimum_order_units, quantity),
        )
        if quantity
        else 0
    )


def candidates(
    snapshot: StoreSnapshot, forecast: Forecast, config: ProjectConfig
) -> tuple[Candidate, ...]:
    product = next((p for p in snapshot.products if p.sku == forecast.sku), None)
    if product is None:
        raise ValueError("unknown forecast SKU")
    q = order_up_to(snapshot, forecast, config)
    pack = config.operating.case_pack
    quantities = sorted(
        {0, q, min(config.operating.maximum_order_units, math.ceil(q * 1.5 / pack) * pack)}
    )
    prices = sorted(
        {product.price_cents}
        | {(product.reference_price_cents * n + 50) // 100 for n in (90, 95, 100, 105, 110)}
    )
    result = []
    for price in prices:
        for quantity in quantities:
            actions = []
            if price != product.price_cents:
                actions.append(SetPriceAction(sku=product.sku, price_cents=price))
            if quantity:
                actions.append(CreatePurchaseOrderAction(sku=product.sku, quantity=quantity))
            check = validate_plan(
                snapshot, actions, config, expected_snapshot_version=snapshot.snapshot_version
            )
            if check["feasible"]:
                result.append(Candidate(product.sku, price, quantity, tuple(actions)))
    if len(result) > 18:
        raise RuntimeError("candidate cap exceeded")
    return tuple(result)


class PlanningModel:
    """One immutable-snapshot comparison; caches never survive another snapshot."""

    def __init__(
        self, snapshot: StoreSnapshot, forecasts: tuple[Forecast, ...], config: ProjectConfig
    ):
        self.snapshot, self.config = snapshot, config
        self.forecasts = {f.sku: f for f in forecasts}
        known = {p.sku for p in snapshot.products}
        if (
            len(self.forecasts) != len(forecasts)
            or not self.forecasts
            or not self.forecasts.keys() <= known
        ):
            raise ValueError("forecasts must name unique known SKUs")
        lengths = {len(f.points) for f in forecasts}
        cutoffs = {f.cutoff for f in forecasts}
        if lengths != {config.planning.horizon} or len(cutoffs) != 1:
            raise ValueError("forecast cutoff/horizon mismatch")
        for f in forecasts:
            from datetime import timedelta

            if any(p.date != f.cutoff + timedelta(days=i + 1) for i, p in enumerate(f.points)):
                raise ValueError("forecast dates must follow cutoff consecutively")
        self._demands = {}
        self._scores = {}

    def _score_sku(self, candidate: Candidate, elasticity: float):
        key = candidate.sku, candidate.price_cents, candidate.order_units, elasticity
        if key in self._scores:
            return self._scores[key]
        snapshot, config = self.snapshot, self.config
        product = next(p for p in snapshot.products if p.sku == candidate.sku)
        stock = next(i.stock for i in snapshot.inventory if i.sku == candidate.sku)
        lead = next(
            s.lead_time_days for s in snapshot.suppliers if s.supplier_id == product.supplier_id
        )
        forecast = self.forecasts[candidate.sku]
        draw_key = candidate.sku, candidate.price_cents, elasticity
        if draw_key not in self._demands:
            streams = []
            for replicate in range(config.planning.rollout_count):
                stream = []
                for point in forecast.points:
                    mean = (
                        point.mean_units
                        * (candidate.price_cents / product.price_cents) ** elasticity
                    )
                    # Planning namespace/seed never use the evaluator's seed or future draws.
                    # Keyed by simulated day, not snapshot version: executions bump the version,
                    # and planner randomness must not depend on how many changes were committed.
                    u = uniform(
                        config.planning.planning_seed,
                        "planning-rollout",
                        snapshot.tenant_id,
                        snapshot.day,
                        snapshot.policy_version,
                        candidate.sku,
                        str(point.date),
                        replicate,
                    )
                    stream.append(
                        negative_binomial_quantile(
                            u, mean, float(config.simulation.demand_dispersion)
                        )
                    )
                streams.append(tuple(stream))
            self._demands[draw_key] = tuple(streams)
        arrivals = {}
        for order in snapshot.open_purchase_orders:
            if order.sku == candidate.sku and order.status == "pending":
                offset = max(1, order.visible_eta_day - snapshot.day)
                arrivals[offset] = arrivals.get(offset, 0) + order.quantity
        if candidate.order_units:
            arrivals[lead] = arrivals.get(lead, 0) + candidate.order_units
        outcomes = []
        for stream in self._demands[draw_key]:
            available = stock
            profit = -config.operating.order_fee_cents * MILLI if candidate.order_units else 0
            fulfilled, demand = 0, 0
            for offset, units in enumerate(stream):
                available += arrivals.get(offset, 0)
                sold = min(available, units)
                available -= sold
                fulfilled += sold
                demand += units
                profit += (candidate.price_cents - product.cost_cents) * sold * MILLI
                profit -= (
                    product.cost_cents
                    * available
                    * int(config.simulation.daily_holding_rate * MILLI)
                )
            outcomes.append((profit, fulfilled, demand))
        self._scores[key] = tuple(outcomes)
        return self._scores[key]

    def score(self, bundle: tuple[Candidate, ...], elasticity=None) -> dict:
        if {c.sku for c in bundle} != set(self.forecasts) or len(bundle) != len(self.forecasts):
            raise ValueError("score requires exactly one candidate per forecast SKU")
        elasticity = (
            float(self.config.planning.central_elasticity) if elasticity is None else elasticity
        )
        totals = [[0, 0, 0] for _ in range(self.config.planning.rollout_count)]
        for candidate in bundle:
            for total, score in zip(totals, self._score_sku(candidate, elasticity), strict=True):
                for i, value in enumerate(score):
                    total[i] += value
        profits = sorted(row[0] for row in totals)
        fulfilled = sum(row[1] for row in totals)
        demand = sum(row[2] for row in totals)
        return {
            "expected_profit_millicents": sum(profits) / len(profits),
            "profit_p10_millicents": profits[max(0, math.ceil(0.1 * len(profits)) - 1)],
            "profit_p90_millicents": profits[math.ceil(0.9 * len(profits)) - 1],
            "expected_fill_rate": fulfilled / demand if demand else None,
            "rollouts": len(profits),
            "horizon_days": self.config.planning.horizon,
            "elasticity_assumption": elasticity,
        }


def compare_plans(
    snapshot: StoreSnapshot, forecasts: tuple[Forecast, ...], config: ProjectConfig
) -> dict:
    model = PlanningModel(snapshot, forecasts, config)
    products = {p.sku: p for p in snapshot.products}
    no_ops = {f.sku: Candidate(f.sku, products[f.sku].price_cents, 0, ()) for f in forecasts}
    baseline = model.score(tuple(no_ops.values()))
    rankings = []
    candidate_summaries = []
    for forecast in sorted(forecasts, key=lambda f: f.sku):
        sku = forecast.sku
        options = candidates(snapshot, forecast, config)
        single_base = (
            sum(
                r[0]
                for r in model._score_sku(no_ops[sku], float(config.planning.central_elasticity))
            )
            / config.planning.rollout_count
        )
        for candidate in options:
            value = (
                sum(
                    r[0]
                    for r in model._score_sku(candidate, float(config.planning.central_elasticity))
                )
                / config.planning.rollout_count
            )
            gain = value - single_base
            candidate_summaries.append(
                {
                    "sku": sku,
                    "price_cents": candidate.price_cents,
                    "order_units": candidate.order_units,
                    "incremental_profit_millicents": gain,
                }
            )
            if candidate.actions and gain > 0:
                rankings.append((gain, candidate))
    rankings.sort(key=lambda row: (-row[0], row[1].sku, row[1].price_cents, row[1].order_units))
    chosen = no_ops.copy()
    used = set()
    actions = []
    for _gain, candidate in rankings:
        if candidate.sku in used:
            continue
        proposed = tuple(actions) + candidate.actions
        check = validate_plan(
            snapshot, proposed, config, expected_snapshot_version=snapshot.snapshot_version
        )
        if check["feasible"]:
            chosen[candidate.sku] = candidate
            used.add(candidate.sku)
            actions.extend(candidate.actions)
    result = model.score(tuple(chosen.values()))
    sensitivity = [
        model.score(tuple(chosen.values()), float(e))
        for e in (
            config.planning.sensitivity_elasticity_min,
            config.planning.sensitivity_elasticity_max,
        )
    ]
    check = validate_plan(
        snapshot, actions, config, expected_snapshot_version=snapshot.snapshot_version
    )
    return {
        "planner_version": PLANNER_VERSION,
        "snapshot_version": snapshot.snapshot_version,
        "policy_version": snapshot.policy_version,
        "actions": [a.model_dump() for a in actions],
        "no_op": baseline,
        "selected": result,
        "incremental_profit_millicents": result["expected_profit_millicents"]
        - baseline["expected_profit_millicents"],
        "elasticity_sensitivity": sensitivity,
        "validation": check,
        "candidates": candidate_summaries,
        "warnings": [
            "Approximate open-loop planning estimates, not realized outcomes or global optima.",
            "Visible inbound ETAs are assumed; overdue orders are assumed to arrive next day.",
            "Forecasts are treated as current-price demand and may be stockout-censored.",
        ],
    }
