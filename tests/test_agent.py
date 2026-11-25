"""Phase 3D exit gate with a scripted model: grounded proposals, justified no-ops, safe
escalations, visible tool/model failures, and resume without resetting budgets."""

import json

import pytest
from test_monitoring import FAST
from test_scheduling import investigations, monitor, proactive, scheduler

from ecommerce_agent.agent import TOOL_SPECS, compact, single_agent_controller
from ecommerce_agent.llm import ModelError, ModelReply
from ecommerce_agent.persistence import transaction
from ecommerce_agent.scheduling import InvestigationWorker


def call(name, **args):
    return {"name": name, "args": args}


def reply(*calls, text=""):
    message = {"role": "assistant", "content": text}
    if calls:
        message["tool_calls"] = [
            {
                "id": f"call_{i}_{c['name']}",
                "type": "function",
                "function": {"name": c["name"], "arguments": json.dumps(c["args"])},
            }
            for i, c in enumerate(calls)
        ]
    return ModelReply(message, "tool_calls", 1000, 200, 120, "fake-model")


class Script:
    """A fake gateway: each step is a ModelReply, an exception, or a callable(messages)."""

    def __init__(self, *steps):
        self.steps, self.requests = list(steps), []

    def __call__(self):
        return self

    def complete(self, messages, tools, *, tool_choice, max_output_tokens):
        assert tools == TOOL_SPECS
        self.requests.append(
            {"messages": json.loads(json.dumps(messages)), "tool_choice": tool_choice}
        )
        step = (
            self.steps.pop(0)
            if self.steps
            else reply(call("get_supplier_status", supplier_ids=["supplier-fast"]))
        )
        if isinstance(step, Exception):
            raise step
        return step(messages) if callable(step) else step


def action(kind, sku, price=None, qty=None):
    return {"action_type": kind, "sku": sku, "price_cents": price, "quantity": qty}


def run(env, script, **kwargs):
    proactive(env, controller="single_agent")
    monitor(env)
    scheduler(env).run_once()
    return review(env, script, **kwargs)


def review(env, script, **kwargs):
    controllers = {"single_agent": single_agent_controller(env["history"], script)}
    return InvestigationWorker(env["services"]["agent_0"], "store-a", controllers, **kwargs)


def traces(env):
    with transaction(env["admin"]) as conn:
        return [
            r["data"]
            for r in conn.execute(
                "SELECT data FROM app.thread_records WHERE tenant_id='store-a' ORDER BY sequence"
            ).fetchall()
        ]


ORDER = action("create_purchase_order", FAST, qty=10)


def test_grounded_proposal_is_validated_and_waits_for_the_merchant(env):
    script = Script(
        reply(call("forecast_demand", skus=[FAST])),
        reply(
            call("submit_decision", decision="proposal", explanation="Low cover.", actions=[ORDER])
        ),
    )
    result = run(env, script).run_once()
    assert result["state"] == "PROPOSED"
    opening = json.loads(script.requests[0]["messages"][1]["content"])
    assert opening["store"]["products"][0]["allowed_price_cents"][0] > 0
    forecast = json.loads(script.requests[1]["messages"][-1]["content"])
    assert forecast["forecasts"][0]["sku"] == FAST  # Compact view, not the raw tool payload.
    row = investigations(env)[0]
    assert (row["usage"]["model_calls"], row["usage"]["tool_calls"]) == (2, 3)
    assert row["usage"]["output_tokens"] == 400 and row["usage"]["reasoning_tokens"] == 240
    assert all(e.startswith("tool:") for e in row["outcome"]["evidence_ids"])
    with transaction(env["admin"]) as conn:
        proposal = conn.execute("SELECT status,payload FROM app.proposals").fetchone()
        approvals = conn.execute("SELECT 1 FROM app.approvals").fetchall()
    assert proposal["status"] == "PENDING" and approvals == []
    assert proposal["payload"]["actions"] == [
        {"action_type": "create_purchase_order", "sku": FAST, "quantity": 10}
    ]


def test_ungrounded_and_invalid_submissions_are_rejected_then_corrected(env):
    script = Script(
        reply(call("submit_decision", decision="proposal", explanation="x", actions=[ORDER])),
        reply(
            call("forecast_demand", skus=[FAST]),
            call("no_such_tool"),
            call("validate_plan", actions=[action("set_price", FAST, qty=3)]),
        ),
        reply(
            call(
                "submit_decision",
                decision="proposal",
                explanation="Cut price.",
                actions=[action("set_price", FAST, price=1)],
            )
        ),
        reply(
            call(
                "submit_decision", decision="no_op", explanation="Stock covers demand.", actions=[]
            )
        ),
    )
    result = run(env, script).run_once()
    assert result["state"] == "NO_OP"
    tool_msgs = [m for m in script.requests[-1]["messages"] if m["role"] == "tool"]
    errors = [json.loads(m["content"]).get("error", "") for m in tool_msgs]
    assert "ground the decision first" in errors[0]
    assert "unknown tool" in errors[2] and "price_cents" in errors[3]
    assert "price_range" in errors[4] and "margin" in errors[4]
    usage = investigations(env)[0]["usage"]
    assert usage["invalid_tool_calls"] == 4 and usage["model_calls"] == 4


def test_budget_exhaustion_forces_a_final_submit_then_escalates(env):
    script = Script()  # Keeps asking for supplier status and never submits.
    result = run(env, script).run_once()
    assert result["state"] == "ESCALATED"
    assert "budget ran out" in result["explanation"]
    assert len(script.requests) == 6
    assert script.requests[-1]["tool_choice"]["function"]["name"] == "submit_decision"
    assert all(r["tool_choice"] == "required" for r in script.requests[:-1])
    usage = investigations(env)[0]["usage"]
    assert usage["model_calls"] == 6 and usage["tool_calls"] <= 12


def test_tool_caps_are_enforced_from_persisted_usage(env):
    compares = [reply(call("forecast_demand", skus=[FAST]))] + [
        reply(call("compare_plans", skus=[FAST])) for _ in range(4)
    ]
    script = Script(
        *compares,
        reply(
            call("submit_decision", decision="no_op", explanation="Plans add little.", actions=[])
        ),
    )
    assert run(env, script).run_once()["state"] == "NO_OP"
    usage = investigations(env)[0]["usage"]
    assert usage["compare_calls"] == 3
    last = json.loads(script.requests[-1]["messages"][-1]["content"])
    assert "budget exhausted" in last["error"]


def test_crash_mid_call_resumes_from_checkpoint_and_charges_conservatively(env):
    first = Script(reply(call("forecast_demand", skus=[FAST])), RuntimeError("worker died"))
    worker = run(env, first)
    with pytest.raises(RuntimeError):
        worker.run_once()
    with transaction(env["admin"]) as conn:
        conn.execute("UPDATE app.investigations SET ready_at=clock_timestamp()")
    second = Script(
        reply(call("submit_decision", decision="proposal", explanation="Reorder.", actions=[ORDER]))
    )
    result = review(env, second).run_once()
    assert result["state"] == "PROPOSED"
    resumed = second.requests[0]["messages"]
    assert [m["role"] for m in resumed] == ["system", "user", "assistant", "tool"]
    row = investigations(env)[0]
    usage = row["usage"]
    assert row["attempts"] == 2 and usage["uncertain_calls"] == 1
    assert usage["model_calls"] == 3  # Two in the first attempt, one after resume.
    assert usage["tool_calls"] == 3  # Snapshot and forecast were not redone; plus validation.
    assert usage["output_tokens"] == 200 + 1200 + 200  # Lost call charged at its ceiling.
    kinds = [t["kind"] for t in traces(env)]
    assert kinds.count("agent_start") == 1 and "agent_uncertain" in kinds


def test_gateway_errors_are_visible_retried_once_then_fail_the_attempt(env):
    script = Script(
        ModelError("APITimeoutError", uncertain=True, retryable=True),
        reply(call("forecast_demand", skus=[FAST])),
        ModelError("RateLimitError 429", uncertain=False, retryable=True),
        ModelError("InternalServerError 500", uncertain=False, retryable=True),
    )
    with pytest.raises(ModelError):
        run(env, script).run_once()
    row = investigations(env)[0]
    assert (row["state"], row["last_error"]) == ("QUEUED", "ModelError")
    assert row["usage"]["model_errors"] == 3 and row["usage"]["uncertain_calls"] == 1
    errors = [t["error"] for t in traces(env) if t["kind"] == "agent_model_error"]
    assert errors == ["APITimeoutError", "RateLimitError 429", "InternalServerError 500"]


def test_compact_compare_reports_dollars_and_best_changes():
    def summary(profit):
        return {
            "expected_profit_millicents": profit,
            "profit_p10_millicents": profit - 100_000,
            "profit_p90_millicents": profit + 100_000,
            "expected_fill_rate": 0.91234,
            "elasticity_assumption": -1.0,
        }

    result = {
        "actions": [action("set_price", FAST, price=110)],
        "no_op": summary(1_000_000),
        "selected": summary(1_250_000),
        "incremental_profit_millicents": 250_000,
        "elasticity_sensitivity": [summary(1_200_000)],
        "candidates": [
            {
                "sku": FAST,
                "price_cents": 110,
                "order_units": 0,
                "incremental_profit_millicents": 250_000,
            },
            {"sku": FAST, "price_cents": 90, "order_units": 0, "incremental_profit_millicents": -5},
        ],
        "validation": {"feasible": True, "procurement_spend_cents": 0},
    }
    view = compact("compare_plans", {"result": result})
    assert view["incremental_profit_usd"] == 2.5 and view["recommended"]["fill_rate"] == 0.912
    assert view["best_single_change_per_sku"] == {
        FAST: {"price_cents": 110, "order_units": 0, "gain_usd": 2.5}
    }
