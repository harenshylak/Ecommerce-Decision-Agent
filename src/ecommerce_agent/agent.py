"""Phase 3D: the single adaptive agent.

A bounded tool loop over the course gateway. The model chooses which permitted numerical tools to
call and finishes with `submit_decision`: a proposal, a justified no-op, or an escalation.
Arithmetic, forecasts, plan scoring, and policy checks stay in tools; every proposal must pass
`validate_plan` and only touch SKUs the agent forecast. Approval and execution are unchanged.

Durability: every step is saved as an investigation trace before the loop moves on. A model call
is recorded (and counted) before it is sent; if the worker dies mid-call, the retry charges the
reserved maximum for that call as uncertain. A retry rebuilds the conversation from the traces
and continues; budgets are read from the persisted usage, so they never reset.
"""

from __future__ import annotations

import json
import math

from ecommerce_agent.controllers import tools_for
from ecommerce_agent.forecasting import SalesHistory
from ecommerce_agent.llm import GatewayClient, ModelError
from ecommerce_agent.scheduling import InvestigationContext, Outcome
from ecommerce_agent.validation import normalize_actions

AGENT_VERSION = "single-agent-0.1.0"
MIN_OUTPUT_TOKENS = 400  # Below this, another call cannot produce a useful decision.
# Per-call output ceiling (reasoning included). It is also the amount reserved, and charged if a
# call's fate is unknown, so one interrupted call cannot consume the whole investigation budget.
MAX_OUTPUT_PER_CALL = 1200
MILLI_PER_USD = 100_000

ANALYSIS_TOOLS = {"forecast_demand", "compare_plans"}

SYSTEM_PROMPT = """You are the review agent for a small online store. Each review you decide \
whether to reorder stock, change prices, or make no change. A human merchant approves or rejects \
whatever you propose; you cannot execute anything yourself.

Rules enforced by validate_plan (it is authoritative):
- Prices must stay within the allowed_price_cents range shown for each product (90-110% of the \
reference price and at least {margin}% gross margin). A product's price may change at most once \
every {cooldown} days.
- Purchase orders: multiples of {case} units, {min_order}-{max_order} units, ${fee} fee per \
order, paid from cash. Stock arrives after the supplier lead time.

How to work:
- Use tools for every number. Do not do your own forecasting or profit arithmetic.
- forecast_demand before acting on a SKU. compare_plans ranks a combined price/reorder bundle by \
simulated expected profit over the next 14 days (it needs forecasts for the SKUs you pass).
- You may adopt the compared bundle, trim it, or adjust it for alerts and risks you can justify \
from tool results (for example a supplier delay or a demand spike). Check changes with \
validate_plan.
- Finish by calling submit_decision exactly once: "proposal" with actions, "no_op" when keeping \
everything is best, or "escalation" when the evidence is contradictory or tools fail.
- Keep the explanation short and concrete; cite the tool results you relied on.
You have a small budget of model and tool calls. Be efficient: a typical review is forecast \
-> compare -> (optional validate) -> submit."""

_ACTION_ITEM = {
    "type": "object",
    "properties": {
        "action_type": {"type": "string", "enum": ["set_price", "create_purchase_order"]},
        "sku": {"type": "string"},
        "price_cents": {
            "type": ["integer", "null"],
            "description": "New price; set_price only, otherwise null.",
        },
        "quantity": {
            "type": ["integer", "null"],
            "description": "Units to order; create_purchase_order only, otherwise null.",
        },
    },
    "required": ["action_type", "sku", "price_cents", "quantity"],
    "additionalProperties": False,
}
_SKUS = {"type": "array", "items": {"type": "string"}, "description": "One to ten store SKUs."}


def _tool(name, description, properties):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": list(properties),
                "additionalProperties": False,
            },
        },
    }


TOOL_SPECS = [
    _tool(
        "get_sales_history",
        "Recent daily fulfilled units, price, and stockout days per SKU.",
        {"skus": _SKUS, "lookback_days": {"type": "integer", "description": "7 to 56."}},
    ),
    _tool(
        "get_supplier_status",
        "Supplier lead times and pending purchase orders with ETAs.",
        {"supplier_ids": {"type": "array", "items": {"type": "string"}}},
    ),
    _tool("forecast_demand", "14-day demand forecast per SKU with 80% bands.", {"skus": _SKUS}),
    _tool(
        "compare_plans",
        "Rank a feasible price/reorder bundle for these SKUs by simulated 14-day profit vs no "
        "change, with elasticity sensitivity. Requires forecasts for every SKU passed.",
        {"skus": _SKUS},
    ),
    _tool(
        "validate_plan",
        "Check a list of actions against store policy, cash, and cooldowns.",
        {"actions": {"type": "array", "items": _ACTION_ITEM}},
    ),
    _tool(
        "submit_decision",
        "Finish the review. Proposals are validated; rejected submissions explain why.",
        {
            "decision": {"type": "string", "enum": ["proposal", "no_op", "escalation"]},
            "explanation": {"type": "string"},
            "actions": {
                "type": "array",
                "items": _ACTION_ITEM,
                "description": "Required for proposal; empty otherwise.",
            },
        },
    ),
]


class SubmissionRejected(ValueError):
    pass


def _usd(millicents):
    return round(millicents / MILLI_PER_USD, 2)


def _price_range(product, policy):
    low = max(
        math.ceil(product.reference_price_cents * policy.minimum_price_percent / 100),
        math.ceil(product.cost_cents * 100 / (100 - policy.minimum_gross_margin_percent)),
    )
    return [low, product.reference_price_cents * policy.maximum_price_percent // 100]


def compact_snapshot(snapshot, config):
    stock = {i.sku: i.stock for i in snapshot.inventory}
    inbound = {}
    for order in snapshot.open_purchase_orders:
        inbound[order.sku] = inbound.get(order.sku, 0) + order.quantity
    cooldown = config.operating.price_cooldown_days
    return {
        "day": snapshot.day,
        "cash_usd": round(snapshot.cash_cents / 100, 2),
        "suppliers": [
            {"supplier_id": s.supplier_id, "lead_time_days": s.lead_time_days}
            for s in snapshot.suppliers
        ],
        "products": [
            {
                "sku": p.sku,
                "supplier_id": p.supplier_id,
                "stock": stock.get(p.sku, 0),
                "inbound_units": inbound.get(p.sku, 0),
                "price_cents": p.price_cents,
                "cost_cents": p.cost_cents,
                "allowed_price_cents": _price_range(p, config.operating),
                "price_locked": p.last_price_change_day is not None
                and snapshot.day - p.last_price_change_day < cooldown,
            }
            for p in sorted(snapshot.products, key=lambda p: p.sku)
        ],
        "open_purchase_orders": [
            {
                "order_id": o.order_id,
                "sku": o.sku,
                "quantity": o.quantity,
                "eta_day": o.visible_eta_day,
            }
            for o in snapshot.open_purchase_orders
        ],
    }


def _profit(summary):
    return {
        "expected_profit_usd": _usd(summary["expected_profit_millicents"]),
        "p10_usd": _usd(summary["profit_p10_millicents"]),
        "p90_usd": _usd(summary["profit_p90_millicents"]),
        "fill_rate": round(summary["expected_fill_rate"], 3),
    }


def compact(name, response):
    """What the model sees. The full tool response is kept in the trace as evidence."""
    result = response["result"]
    if name == "forecast_demand":
        rows = []
        for f in result:
            points = f["points"]
            rows.append(
                {
                    "sku": f["sku"],
                    "next7_units": round(sum(p["mean_units"] for p in points[:7]), 1),
                    "next14_units": round(sum(p["mean_units"] for p in points), 1),
                    "next14_low80": round(sum(p["lower_80_units"] for p in points), 1),
                    "next14_high80": round(sum(p["upper_80_units"] for p in points), 1),
                }
                | ({"warnings": f["warnings"]} if f["warnings"] else {})
            )
        return {"forecasts": rows}
    if name == "compare_plans":
        best = {}
        for c in result["candidates"]:
            gain = c["incremental_profit_millicents"]
            if gain > 0 and gain > best.get(c["sku"], {}).get("_gain", 0):
                best[c["sku"]] = {
                    "price_cents": c["price_cents"],
                    "order_units": c["order_units"],
                    "gain_usd": _usd(gain),
                    "_gain": gain,
                }
        return {
            "recommended_actions": result["actions"],
            "no_change": _profit(result["no_op"]),
            "recommended": _profit(result["selected"]),
            "incremental_profit_usd": _usd(result["incremental_profit_millicents"]),
            "elasticity_sensitivity": [
                {"elasticity": s["elasticity_assumption"]} | _profit(s)
                for s in result["elasticity_sensitivity"]
            ],
            "best_single_change_per_sku": {
                sku: {k: v for k, v in b.items() if k != "_gain"} for sku, b in best.items()
            },
            "feasible": result["validation"]["feasible"],
            "procurement_spend_usd": round(
                result["validation"]["procurement_spend_cents"] / 100, 2
            ),
        }
    if name == "get_sales_history":
        by_sku = {}
        for r in result:
            row = by_sku.setdefault(r["sku"], {"units": [], "stockout_days": 0, "price": None})
            row["units"].append(r["sales_units"])
            row["stockout_days"] += bool(r.get("ended_without_stock"))
            row["price"] = r.get("price_cents") or row["price"]
        return {
            "history": [
                {
                    "sku": sku,
                    "daily_units_oldest_first": v["units"],
                    "avg_units": round(sum(v["units"]) / len(v["units"]), 2),
                    "stockout_days": v["stockout_days"],
                    "latest_price_cents": v["price"],
                }
                for sku, v in by_sku.items()
            ]
        }
    if name == "validate_plan":
        return {
            "feasible": result["feasible"],
            "violations": result["violations"],
            "procurement_spend_usd": round(result["procurement_spend_cents"] / 100, 2),
        }
    return result


def actions_from(items):
    """Strict-schema action items (nullable fields) to contract actions."""
    values = []
    for item in items:
        kind = item.get("action_type")
        if kind == "set_price":
            if item.get("price_cents") is None:
                raise ValueError("set_price needs price_cents")
            values.append(
                {"action_type": kind, "sku": item["sku"], "price_cents": item["price_cents"]}
            )
        elif kind == "create_purchase_order":
            if item.get("quantity") is None:
                raise ValueError("create_purchase_order needs quantity")
            values.append({"action_type": kind, "sku": item["sku"], "quantity": item["quantity"]})
        else:
            raise ValueError(f"unknown action_type {kind!r}")
    return normalize_actions(values)


def _estimate_tokens(messages):
    """Conservative pre-call estimate (JSON characters / 3) for reserving input allowance."""
    return (len(json.dumps(messages)) + len(json.dumps(TOOL_SPECS))) // 3


class AgentRun:
    def __init__(self, context: InvestigationContext, tools, client):
        self.c, self.tools, self.client = context, tools, client
        self.limits = context.config.model
        # The persisted investigation usage is the real budget; lift the per-instance tool counter
        # so a resumed run's replays and the mandatory submit validation are never refused here.
        model = self.limits.model_copy(update={"max_tool_calls": 10**6, "max_compare_calls": 10**6})
        tools.config = tools.config.model_copy(update={"model": model})
        self.messages: list[dict] = []
        self.evidence: list[str] = []
        self.used_tools: set[str] = set()
        self.decision: Outcome | None = None
        self.consecutive_errors = 0

    # ---- persistence -------------------------------------------------------------------------
    def _start(self):
        snapshot = self.tools.get_store_snapshot()
        self.c.charge(tool_calls=1)
        request = self.c.request
        alerts = [
            {k: a.get(k) for k in ("alert_type", "sku", "order_id", "severity", "evidence")}
            for a in request.get("alerts", [])
        ]
        opening = {
            "objective": request.get("objective") or request.get("question"),
            "review_day": self.c.snapshot.day,
            "open_alerts": alerts,
            "store": compact_snapshot(self.c.snapshot, self.c.config),
        }
        policy = self.c.config.operating
        system = SYSTEM_PROMPT.format(
            margin=policy.minimum_gross_margin_percent,
            cooldown=policy.price_cooldown_days,
            case=policy.case_pack,
            min_order=policy.minimum_order_units,
            max_order=policy.maximum_order_units,
            fee=policy.order_fee_cents / 100,
        )
        self.messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(opening)},
        ]
        self.evidence.append(snapshot["tool_call_id"])
        self.c.trace(
            {
                "kind": "agent_start",
                "agent_version": AGENT_VERSION,
                "messages": self.messages,
                "tool_call_id": snapshot["tool_call_id"],
                "response": snapshot,
            }
        )

    def load(self):
        """Rebuild the conversation from saved traces, or start a new one."""
        traces = [t for t in self.c.traces() if str(t.get("kind", "")).startswith("agent_")]
        if not traces:
            self._start()
            return
        open_request = None
        for t in traces:
            kind = t["kind"]
            if kind == "agent_start":
                self.messages = list(t["messages"])
                self.evidence.append(t["tool_call_id"])
            elif kind == "agent_model_request":
                open_request = t
            elif kind in ("agent_model_response", "agent_model_error", "agent_uncertain"):
                open_request = None
                if kind == "agent_model_response":
                    self.messages.append(t["message"])
            elif kind == "agent_note":
                self.messages.append({"role": "user", "content": t["content"]})
            elif kind == "agent_tool":
                self.messages.append(
                    {"role": "tool", "tool_call_id": t["call_id"], "content": t["content"]}
                )
                if t.get("tool_call_id"):
                    self.evidence.append(t["tool_call_id"])
                    self.used_tools.add(t["name"])
                if t["name"] == "forecast_demand" and t.get("tool_call_id"):
                    self.tools.forecast(t["arguments"]["skus"])  # Restore server-side forecasts.
            elif kind == "agent_decision":
                self.decision = Outcome(
                    t["outcome"],
                    t["explanation"],
                    tuple(normalize_actions(t["actions"])),
                    tuple(t["evidence_ids"]),
                )
        if open_request is not None:  # Died mid-call: the provider may have billed it.
            self.c.charge(
                input_tokens=open_request["reserved_input"],
                output_tokens=open_request["reserved_output"],
                uncertain_calls=1,
            )
            self.c.trace({"kind": "agent_uncertain", "n": open_request["n"]})

    # ---- tools -------------------------------------------------------------------------------
    def _pending_calls(self):
        """Tool calls of the latest model turn that have no saved result yet."""
        index = next(
            (
                i
                for i in range(len(self.messages) - 1, -1, -1)
                if self.messages[i]["role"] == "assistant"
            ),
            None,
        )
        if index is None or not self.messages[index].get("tool_calls"):
            return []
        answered = {
            m.get("tool_call_id") for m in self.messages[index + 1 :] if m["role"] == "tool"
        }
        return [c for c in self.messages[index]["tool_calls"] if c["id"] not in answered]

    def _reply(self, call, name, arguments, content, response=None):
        content = json.dumps(content)
        self.messages.append({"role": "tool", "tool_call_id": call["id"], "content": content})
        self.c.trace(
            {
                "kind": "agent_tool",
                "call_id": call["id"],
                "name": name,
                "arguments": arguments,
                "content": content,
                "tool_call_id": response["tool_call_id"] if response else None,
                "response": response,
            }
        )
        if response:
            self.evidence.append(response["tool_call_id"])
            self.used_tools.add(name)

    def _run_tool(self, name, args):
        if name == "get_sales_history":
            lookback = min(56, max(7, int(args["lookback_days"])))
            return self.tools.get_sales_history(args["skus"], lookback)
        if name == "get_supplier_status":
            return self.tools.get_supplier_status(args["supplier_ids"])
        if name == "forecast_demand":
            return self.tools.forecast(args["skus"])
        if name == "compare_plans":
            return self.tools.compare(args["skus"])
        if name == "validate_plan":
            return self.tools.validate(actions_from(args["actions"]))
        raise ValueError(f"unknown tool {name!r}")

    def _call_tool(self, call):
        self.c.check_deadline()
        name = call["function"]["name"]
        try:
            args = json.loads(call["function"]["arguments"] or "{}")
        except json.JSONDecodeError:
            self.c.charge(invalid_tool_calls=1)
            self._reply(call, name, None, {"error": "arguments were not valid JSON"})
            return None
        if name == "submit_decision":
            return self._submit(call, args)
        usage = self.c.usage
        if usage.get("tool_calls", 0) >= self.limits.max_tool_calls or (
            name == "compare_plans"
            and usage.get("compare_calls", 0) >= self.limits.max_compare_calls
        ):
            self._reply(call, name, args, {"error": "tool budget exhausted; call submit_decision"})
            return None
        try:
            response = self._run_tool(name, args)
        except (ValueError, TypeError, KeyError) as exc:
            self.c.charge(tool_calls=1, invalid_tool_calls=1)
            self._reply(call, name, args, {"error": str(exc)[:300]})
            return None
        self.c.charge(tool_calls=1, compare_calls=int(name == "compare_plans"))
        self._reply(call, name, args, compact(name, response), response)
        return None

    def _check_submission(self, args):
        decision = args.get("decision")
        explanation = str(args.get("explanation", "")).strip()
        if decision not in ("proposal", "no_op", "escalation") or not explanation:
            raise SubmissionRejected("decision must be proposal/no_op/escalation with explanation")
        if decision != "escalation" and not self.used_tools & ANALYSIS_TOOLS:
            raise SubmissionRejected(
                "ground the decision first: call forecast_demand and/or compare_plans"
            )
        if decision != "proposal":
            if args.get("actions"):
                raise SubmissionRejected(f"{decision} must not carry actions")
            return Outcome(decision, explanation, (), tuple(self.evidence))
        actions = tuple(actions_from(args.get("actions") or []))
        if not actions:
            raise SubmissionRejected("a proposal needs at least one action")
        unforecast = sorted({a.sku for a in actions} - set(self.tools._forecasts))
        if unforecast:
            raise SubmissionRejected(f"forecast these SKUs before acting on them: {unforecast}")
        check = self.tools.validate(actions)  # Mandatory; charged like any validation.
        self.c.charge(tool_calls=1)
        self.evidence.append(check["tool_call_id"])
        if not check["result"]["feasible"]:
            raise SubmissionRejected(
                "proposal failed validation: " + json.dumps(check["result"]["violations"])[:400]
            )
        return Outcome("proposal", explanation, actions, tuple(self.evidence))

    def _submit(self, call, args):
        try:
            outcome = self._check_submission(args)
        except (SubmissionRejected, ValueError, TypeError, KeyError) as exc:
            self.c.charge(invalid_tool_calls=1)
            self._reply(call, "submit_decision", args, {"accepted": False, "error": str(exc)[:500]})
            return None
        self._reply(call, "submit_decision", args, {"accepted": True})
        self.c.trace(
            {
                "kind": "agent_decision",
                "outcome": outcome.kind,
                "explanation": outcome.explanation,
                "actions": [a.model_dump() for a in outcome.actions],
                "evidence_ids": list(outcome.evidence_ids),
            }
        )
        return outcome

    # ---- model -------------------------------------------------------------------------------
    def _call_model(self):
        """One budgeted model call. Returns False when the budget cannot afford another call."""
        self.c.check_deadline()
        usage = self.c.usage
        calls = usage.get("model_calls", 0)
        remaining_output = min(
            MAX_OUTPUT_PER_CALL, self.limits.max_output_tokens - usage.get("output_tokens", 0)
        )
        estimate = _estimate_tokens(self.messages)
        if (
            calls >= self.limits.max_model_calls
            or remaining_output < MIN_OUTPUT_TOKENS
            or usage.get("input_tokens", 0) + estimate > self.limits.max_input_tokens
        ):
            return False
        final = (
            calls + 1 >= self.limits.max_model_calls
            or usage.get("input_tokens", 0) + 2 * estimate > self.limits.max_input_tokens
        )
        choice = (
            {"type": "function", "function": {"name": "submit_decision"}} if final else "required"
        )
        n = calls + 1
        self.c.trace(
            {
                "kind": "agent_model_request",
                "n": n,
                "reserved_input": estimate,
                "reserved_output": remaining_output,
                "forced_submit": final,
            }
        )
        self.c.charge(model_calls=1)  # Counted before sending: a crash cannot hide the call.
        try:
            reply = self.client.complete(
                self.messages, TOOL_SPECS, tool_choice=choice, max_output_tokens=remaining_output
            )
        except ModelError as exc:
            extra = (
                {"input_tokens": estimate, "output_tokens": remaining_output, "uncertain_calls": 1}
                if exc.uncertain
                else {}
            )
            self.c.charge(model_errors=1, **extra)
            self.c.trace({"kind": "agent_model_error", "n": n, "error": str(exc)})
            self.consecutive_errors += 1
            if not exc.retryable or self.consecutive_errors >= 2:
                raise  # The worker releases the lease; a later attempt resumes from here.
            return True
        self.consecutive_errors = 0
        self.c.charge(
            input_tokens=reply.input_tokens or estimate,
            output_tokens=reply.output_tokens,
            reasoning_tokens=reply.reasoning_tokens,
        )
        self.c.trace(
            {
                "kind": "agent_model_response",
                "n": n,
                "message": reply.message,
                "finish_reason": reply.finish_reason,
                "model": reply.model,
                "usage": {
                    "input_tokens": reply.input_tokens,
                    "output_tokens": reply.output_tokens,
                    "reasoning_tokens": reply.reasoning_tokens,
                },
            }
        )
        self.messages.append(reply.message)
        if not reply.message.get("tool_calls"):
            note = "Reply only with tool calls. Finish with submit_decision."
            self.messages.append({"role": "user", "content": note})
            self.c.trace({"kind": "agent_note", "content": note})
        return True

    def run(self) -> Outcome:
        self.load()
        while self.decision is None:
            for call in self._pending_calls():
                outcome = self._call_tool(call)
                if outcome is not None:
                    self.decision = outcome
                    break
            if self.decision is not None:
                break
            if not self._call_model():
                return Outcome(
                    "escalation",
                    "The agent's review budget ran out before a validated decision; "
                    "a merchant should review.",
                    evidence_ids=tuple(self.evidence),
                )
        return self.decision


def single_agent_controller(base_history: SalesHistory, client_factory=GatewayClient):
    def run(context: InvestigationContext) -> Outcome:
        tools = tools_for(context, base_history)
        return AgentRun(context, tools, client_factory()).run()

    return run
