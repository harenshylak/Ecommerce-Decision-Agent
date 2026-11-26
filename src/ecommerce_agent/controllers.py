"""Decision controllers that run inside the investigation worker.

Every controller receives the same InvestigationContext (released snapshot, completed public
sales, policy config, scheduled alerts) and the same numerical tools. Each returns an Outcome;
proposals still pass through validation, merchant approval and the restricted executor.
"""

from __future__ import annotations

from datetime import timedelta

from ecommerce_agent.controls import NumericalTools
from ecommerce_agent.forecasting import SalesHistory
from ecommerce_agent.monitoring import history_until
from ecommerce_agent.scheduling import InvestigationContext, Outcome
from ecommerce_agent.validation import normalize_actions


def tools_for(context: InvestigationContext, base_history: SalesHistory) -> NumericalTools:
    """Scoped tools over training history plus this store's completed simulated sales."""
    snapshot = context.snapshot
    cutoff = context.calendar_origin + timedelta(days=snapshot.day - 2)
    merged = history_until(base_history, context.observed_sales, cutoff)
    history = SalesHistory(context.tenant, merged.records, merged.source_id)
    return NumericalTools(snapshot, history, cutoff + timedelta(days=1), context.config)


class TracedTools:
    """Persist each tool response as a trace and charge it to the investigation's usage."""

    def __init__(self, context: InvestigationContext, tools: NumericalTools):
        self.context, self.tools = context, tools

    def call(self, name, *args, **kwargs):
        self.context.check_deadline()
        response = getattr(self.tools, name)(*args, **kwargs)
        self.context.charge(tool_calls=1, compare_calls=int(name == "compare"))
        self.context.trace({"kind": "tool", "tool": response["tool"], "response": response})
        return response


def fixed_controller(base_history: SalesHistory):
    """The fixed numerical workflow: snapshot, forecast, compare, validate. No model calls."""

    def run(context: InvestigationContext) -> Outcome:
        tools = TracedTools(context, tools_for(context, base_history))
        skus = sorted(p.sku for p in context.snapshot.products)
        tools.call("get_store_snapshot")
        tools.call("forecast", skus)
        ranked = tools.call("compare", skus)["result"]
        actions = normalize_actions(ranked["actions"])
        check = tools.call("validate", actions)
        evidence = tuple(t["tool_call_id"] for t in tools.tools.traces)
        if not check["result"]["feasible"]:
            return Outcome(
                "escalation",
                "The top-ranked plan failed validation; a merchant should review.",
                evidence_ids=evidence,
            )
        if not actions:
            return Outcome(
                "no_op",
                "Keeping current prices and orders scored best of the feasible plans.",
                evidence_ids=evidence,
            )
        return Outcome(
            "proposal",
            "Selected the feasible tool-ranked bundle with the best expected profit.",
            actions,
            evidence,
        )

    return run


def controller_registry(base_history: SalesHistory, client_factory=None) -> dict:
    """All three decision makers, sharing tools, observations, budgets and approval path."""
    from ecommerce_agent.agent import single_agent_controller
    from ecommerce_agent.llm import GatewayClient
    from ecommerce_agent.multi_agent import multi_agent_controller

    client_factory = client_factory or GatewayClient
    return {
        "fixed": fixed_controller(base_history),
        "single_agent": single_agent_controller(base_history, client_factory),
        "multi_agent": multi_agent_controller(base_history, client_factory),
    }
