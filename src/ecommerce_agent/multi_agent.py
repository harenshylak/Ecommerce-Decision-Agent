"""Phase 3E: the bounded multi-agent team.

A supervisor reads the alerts and decides which specialists to consult. Each consultation is a
short specialist conversation with its own narrow tools that ends in `submit_recommendation`.
Specialists cannot submit decisions, approve, or execute. The supervisor owns plan comparison,
validation, and the single final `submit_decision` under the store's shared cash.

The whole team shares one investigation budget (model calls, tokens, tools, time); delegations
are counted too. Only the output-token cap is larger than the single agent's (see below).
Every step is saved per conversation, so a crash resumes the exact specialist
or supervisor turn without repeating or refunding anything.
"""

from __future__ import annotations

from ecommerce_agent.agent import (
    _ACTION_ITEM,
    _SKUS,
    RULES,
    TOOL_SPECS,
    Conversation,
    SubmissionRejected,
    Team,
    _estimate_tokens,
    _tool,
    actions_from,
    compact,
    policy_values,
    run_decision_conversation,
)
from ecommerce_agent.controllers import tools_for
from ecommerce_agent.forecasting import SalesHistory
from ecommerce_agent.llm import GatewayClient
from ecommerce_agent.scheduling import InvestigationContext, Outcome

TEAM_VERSION = "multi-agent-0.1.0"
SPECIALIST_CALLS = 2  # Model calls per consultation: usually forecast, then recommend.
SUPERVISOR_INPUT_RESERVE = 1500  # Room for the specialist's answer in the supervisor's context.
SUPERVISOR_OUTPUT_RESERVE = 1000  # Output kept for the supervisor's own final decision.
SPECIALIST_OUTPUT_PER_CALL = 1000
# Decision (pilot, 2026-11-25): the team gets a larger output-token cap than the single agent
# (5,000 vs 3,000, reasoning included) because three reasoning roles exhausted 3,000 in ~40% of
# reviews. All other caps are shared with the single agent. Disclose this in the report.
TEAM_MAX_OUTPUT_TOKENS = 5000

_SPEC = {s["function"]["name"]: s for s in TOOL_SPECS}

SUPERVISOR_PROMPT = (
    """You supervise a small review team for an online store. Each review the team \
decides whether to reorder stock, change prices, or make no change. A human merchant approves or \
rejects the final proposal; nobody on the team can execute anything.

"""
    + RULES
    + """

Your team:
- ask_inventory_specialist: forecasts demand, checks suppliers, recommends purchase orders.
- ask_pricing_specialist: forecasts demand, checks sales history, recommends price changes.
Specialists forecast the SKUs they study; you can only compare or act on forecast SKUs.

How to work:
- Read the alerts and store table, then consult the specialist(s) the situation needs with a \
focused question and the relevant SKUs. When both matter, ask both in the same turn.
- Then reconcile: use compare_plans on forecast SKUs to check expected profit, combine the \
recommendations into one bundle that fits the shared cash, and check it with validate_plan.
- Finish by calling submit_decision exactly once: "proposal" with actions, "no_op" when keeping \
everything is best, or "escalation" when the evidence is contradictory or tools fail.
- Keep the explanation short; say which specialist advice and tool results you relied on.
The whole team shares six model calls, and each consultation uses up to two of them. A typical \
review is: ask both specialists -> compare/validate -> submit."""
)

SPECIALIST_PROMPT = (
    """You are the {role} specialist on a store review team. The supervisor asked you a \
question. {scope} You only recommend; the supervisor makes the final decision.

"""
    + RULES
    + """

How to work:
- Use tools for every number. Forecasts for the focus SKUs are already in your briefing; call \
forecast_demand only for other SKUs you need.
- {tools_hint}
- Finish with submit_recommendation: the {actions} you recommend (an empty list if none are \
needed) and a rationale of at most 40 words citing the numbers you used.
Be brief and decisive. Usually you can recommend in your first reply."""
)

ROLES = {
    "inventory": {
        "tools": ["forecast_demand", "get_sales_history", "get_supplier_status", "validate_plan"],
        "action_type": "create_purchase_order",
        "scope": "You decide which products need purchase orders and how many units.",
        "tools_hint": "get_supplier_status shows lead times and pending orders with ETAs.",
        "actions": "purchase orders (create_purchase_order only)",
    },
    "pricing": {
        "tools": ["forecast_demand", "get_sales_history", "validate_plan"],
        "action_type": "set_price",
        "scope": "You decide which prices should change and to what.",
        "tools_hint": "Respect allowed_price_cents and price_locked in the store table.",
        "actions": "price changes (set_price only)",
    },
}

RECOMMEND_SPEC = _tool(
    "submit_recommendation",
    "Return your recommendation to the supervisor.",
    {"rationale": {"type": "string"}, "actions": {"type": "array", "items": _ACTION_ITEM}},
)


def _ask_spec(role):
    return _tool(
        f"ask_{role}_specialist",
        f"Consult the {role} specialist. Uses up to {SPECIALIST_CALLS} of the team's model calls.",
        {"question": {"type": "string"}, "skus": _SKUS},
    )


SUPERVISOR_SPECS = [
    _ask_spec("inventory"),
    _ask_spec("pricing"),
    _SPEC["compare_plans"],
    _SPEC["validate_plan"],
    _SPEC["submit_decision"],
]


def specialist_specs(role):
    return [_SPEC[name] for name in ROLES[role]["tools"]] + [RECOMMEND_SPEC]


def _recommendation_check(team: Team, role):
    allowed = ROLES[role]["action_type"]

    def finish(conversation, args):
        rationale = str(args.get("rationale", "")).strip()
        if not rationale:
            raise SubmissionRejected("a rationale is required")
        actions = actions_from(args.get("actions") or [])
        if any(a.action_type != allowed for a in actions):
            raise SubmissionRejected(f"the {role} specialist may only recommend {allowed}")
        unforecast = sorted({a.sku for a in actions} - set(team.tools._forecasts))
        if unforecast:
            raise SubmissionRejected(f"forecast these SKUs before recommending: {unforecast}")
        return {"rationale": rationale, "recommended_actions": [a.model_dump() for a in actions]}

    return finish


def prefetch_forecasts(team: Team, conv, skus):
    """Brief a specialist with forecasts for its focus SKUs (a normal, charged tool call).

    SKUs another team member already forecast are reused, not recomputed. Bad SKU lists are
    left for the specialist to handle with its own tools."""
    skus = [s for s in dict.fromkeys(skus or []) if isinstance(s, str)]
    missing = [s for s in skus if s not in team.tools._forecasts]
    if missing:
        usage = team.c.usage
        if usage.get("tool_calls", 0) >= team.limits.max_tool_calls:
            return {}
        try:
            response = team.run_tool("forecast_demand", {"skus": missing})
        except (ValueError, TypeError, KeyError):
            return {}
        team.c.charge(tool_calls=1)
        team.evidence.append(response["tool_call_id"])
        team.used_tools.add("forecast_demand")
        team.c.trace(
            {
                "kind": "agent_prefetch",
                "conv": conv,
                "name": "forecast_demand",
                "arguments": {"skus": missing},
                "tool_call_id": response["tool_call_id"],
                "response": response,
            }
        )
    known = [s for s in skus if s in team.tools._forecasts]
    if not known:
        return {}
    view = compact(
        "forecast_demand",
        {"result": [team.tools._forecasts[s].model_dump(mode="json") for s in known]},
    )
    return {"forecasts": view["forecasts"]}


def delegation_handlers(team: Team):
    def handlers_for(main: Conversation):
        def consult(role):
            def handler(call, args):
                specialist = Conversation(
                    team,
                    f"{role}:{call['id']}",
                    specs=specialist_specs(role),
                    finish=_recommendation_check(team, role),
                    forced_finish="submit_recommendation",
                    output_ceiling=SPECIALIST_OUTPUT_PER_CALL,
                )
                if not specialist.restore():
                    usage, limits = team.c.usage, team.limits
                    remaining = limits.max_model_calls - usage.get("model_calls", 0) - 1
                    if remaining < 1:  # Always keep one call for the supervisor to decide.
                        return {"error": "not enough team budget left to consult; decide now"}
                    specialist.allowance = min(SPECIALIST_CALLS, remaining)
                    team.c.charge(delegations=1)
                    briefing = prefetch_forecasts(team, specialist.conv, args.get("skus"))
                    system = SPECIALIST_PROMPT.format(
                        role=role,
                        **{k: ROLES[role][k] for k in ("scope", "tools_hint", "actions")},
                        **policy_values(team.c.config),
                    )
                    opening = (
                        team.opening()
                        | {
                            "question": str(args.get("question", ""))[:500],
                            "focus_skus": list(args.get("skus") or []),
                        }
                        | briefing
                    )
                    specialist.start(
                        system, opening, allowance=specialist.allowance, team_version=TEAM_VERSION
                    )
                team.reserved_input = (
                    _estimate_tokens(main.messages, SUPERVISOR_SPECS) + SUPERVISOR_INPUT_RESERVE
                )
                team.reserved_output = SUPERVISOR_OUTPUT_RESERVE
                try:
                    result = specialist.run()
                finally:
                    team.reserved_input = team.reserved_output = 0
                if result is None:
                    return {
                        "specialist": role,
                        "recommended_actions": None,
                        "note": "the specialist ran out of budget without a recommendation",
                    }
                return {"specialist": role} | result

            return handler

        return {f"ask_{role}_specialist": consult(role) for role in ROLES}

    return handlers_for


def multi_agent_controller(base_history: SalesHistory, client_factory=GatewayClient):
    def run(context: InvestigationContext) -> Outcome:
        team = Team(context, tools_for(context, base_history), client_factory())
        team.limits = team.limits.model_copy(update={"max_output_tokens": TEAM_MAX_OUTPUT_TOKENS})
        system = SUPERVISOR_PROMPT.format(**policy_values(context.config))
        return run_decision_conversation(
            team, SUPERVISOR_SPECS, system, handlers_for=delegation_handlers(team)
        )

    return run
