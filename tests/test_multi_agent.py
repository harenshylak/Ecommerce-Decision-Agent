"""Phase 3E exit gate with a scripted model: the supervisor delegates, specialists only
recommend within their role, one shared budget covers the team, and crashes resume in place."""

import json

import pytest
from test_agent import ORDER, action, call, reply, traces
from test_monitoring import FAST
from test_scheduling import investigations, monitor, proactive, scheduler

from ecommerce_agent.controllers import controller_registry
from ecommerce_agent.multi_agent import SUPERVISOR_SPECS, multi_agent_controller
from ecommerce_agent.scheduling import InvestigationWorker


def role_of(messages):
    system = messages[0]["content"]
    if system.startswith("You supervise"):
        return "supervisor"
    return "inventory" if "inventory specialist" in system else "pricing"


class Script:
    """Fake gateway; steps are replies, exceptions, or callables(messages) -> reply."""

    def __init__(self, *steps, default=None):
        self.steps, self.default, self.requests = list(steps), default, []

    def __call__(self):
        return self

    def complete(self, messages, tools, *, tool_choice, max_output_tokens):
        self.requests.append(
            {
                "role": role_of(messages),
                "tools": [t["function"]["name"] for t in tools],
                "messages": json.loads(json.dumps(messages)),
                "tool_choice": tool_choice,
            }
        )
        step = self.steps.pop(0) if self.steps else self.default
        if isinstance(step, Exception):
            raise step
        return step(messages) if callable(step) else step


def start(env):
    proactive(env, controller="multi_agent")
    monitor(env)
    scheduler(env).run_once()


def worker(env, script):
    controllers = {"multi_agent": multi_agent_controller(env["history"], script)}
    return InvestigationWorker(env["services"]["agent_0"], "store-a", controllers)


PRICE = action("set_price", FAST, price=210)


def test_supervisor_delegates_and_reconciles_one_validated_proposal(env):
    start(env)
    script = Script(
        reply(
            call("ask_inventory_specialist", question="Reorder?", skus=[FAST]),
            call("ask_pricing_specialist", question="Reprice?", skus=[FAST]),
        ),
        reply(call("forecast_demand", skus=[FAST])),
        reply(call("submit_recommendation", rationale="Cover is low.", actions=[ORDER])),
        reply(call("submit_recommendation", rationale="Demand is steady.", actions=[PRICE])),
        reply(
            call(
                "submit_decision",
                decision="proposal",
                explanation="Both specialists' advice fits cash.",
                actions=[ORDER, PRICE],
            )
        ),
    )
    result = worker(env, script).run_once()
    assert result["state"] == "PROPOSED"
    assert [r["role"] for r in script.requests] == [
        "supervisor",
        "inventory",
        "inventory",
        "pricing",
        "supervisor",
    ]
    tools = {r["role"]: set(r["tools"]) for r in script.requests}
    assert tools["supervisor"] == {s["function"]["name"] for s in SUPERVISOR_SPECS}
    assert "get_supplier_status" in tools["inventory"] and "compare_plans" not in tools["inventory"]
    assert "get_supplier_status" not in tools["pricing"]
    assert not {"submit_decision", "ask_pricing_specialist"} & (
        tools["inventory"] | tools["pricing"]
    )
    advice = [
        json.loads(m["content"]) for m in script.requests[-1]["messages"] if m["role"] == "tool"
    ]
    assert [a["specialist"] for a in advice] == ["inventory", "pricing"]
    briefings = [json.loads(r["messages"][1]["content"]) for r in script.requests[1:4:2]]
    assert [b["forecasts"][0]["sku"] for b in briefings] == [FAST, FAST]  # Pricing reuses it.
    usage = investigations(env)[0]["usage"]
    assert (usage["model_calls"], usage["delegations"]) == (5, 2)
    assert usage["tool_calls"] == 4  # Snapshot, one briefing forecast, a forecast, validation.
    convs = {t.get("conv") for t in traces(env)}
    assert "main" in convs and any(c.startswith("inventory:") for c in convs)


def test_roles_are_enforced_and_the_last_specialist_call_must_recommend(env):
    start(env)
    script = Script(
        reply(
            call("forecast_demand", skus=[FAST]),  # Not a supervisor tool.
            call("ask_pricing_specialist", question="Anything?", skus=[FAST]),
        ),
        reply(
            call("forecast_demand", skus=[FAST]),
            call("submit_recommendation", rationale="Order more.", actions=[ORDER]),
        ),
        reply(call("submit_recommendation", rationale="Small rise.", actions=[PRICE])),
        reply(call("submit_decision", decision="no_op", explanation="Gain too small.", actions=[])),
    )
    assert worker(env, script).run_once()["state"] == "NO_OP"
    pricing = [r for r in script.requests if r["role"] == "pricing"]
    assert pricing[0]["tool_choice"] == "required"
    assert pricing[1]["tool_choice"]["function"]["name"] == "submit_recommendation"
    rejected = json.loads(pricing[1]["messages"][-1]["content"])
    assert "may only recommend set_price" in rejected["error"]
    first_reply = json.loads(script.requests[-1]["messages"][3]["content"])
    assert "for this role" in first_reply["error"]


def test_team_shares_one_budget_and_escalates_when_it_runs_out(env):
    start(env)

    def stubborn(messages):  # Supervisor keeps delegating; specialists never recommend.
        if role_of(messages) == "supervisor":
            return reply(call("ask_inventory_specialist", question="Again?", skus=[FAST]))
        return reply(call("forecast_demand", skus=[FAST]))

    script = Script(default=stubborn)
    result = worker(env, script).run_once()
    assert result["state"] == "ESCALATED"
    usage = investigations(env)[0]["usage"]
    assert usage["model_calls"] == 6 and usage["delegations"] == 2
    assert [r["role"] for r in script.requests] == [
        "supervisor",
        "inventory",
        "inventory",
        "supervisor",
        "inventory",
        "supervisor",
    ]
    final = script.requests[-1]
    assert final["tool_choice"]["function"]["name"] == "submit_decision"
    contents = [t.get("content", "") for t in traces(env) if t["kind"] == "agent_tool"]
    assert any("not enough team budget" in c for c in contents)
    assert any("ran out of budget" in c for c in contents)


def test_crash_inside_a_specialist_resumes_that_specialist(env):
    start(env)
    first = Script(
        reply(call("ask_inventory_specialist", question="Reorder?", skus=[FAST])),
        reply(call("forecast_demand", skus=[FAST])),
        RuntimeError("worker died"),
    )
    with pytest.raises(RuntimeError):
        worker(env, first).run_once()
    from ecommerce_agent.persistence import transaction

    with transaction(env["admin"]) as conn:
        conn.execute("UPDATE app.investigations SET ready_at=clock_timestamp()")
    second = Script(
        reply(call("submit_decision", decision="proposal", explanation="Reorder.", actions=[ORDER]))
    )
    result = worker(env, second).run_once()
    assert result["state"] == "PROPOSED"
    # The lost call used the specialist's second (last) call, so it is not called again; the
    # supervisor resumes with the specialist's saved forecast and a visible "ran out" note.
    assert [r["role"] for r in second.requests] == ["supervisor"]
    advice = json.loads(second.requests[0]["messages"][-1]["content"])
    assert advice["recommended_actions"] is None and "ran out" in advice["note"]
    row = investigations(env)[0]
    assert row["attempts"] == 2
    assert row["usage"]["uncertain_calls"] == 1 and row["usage"]["delegations"] == 1
    # Snapshot, briefing forecast, specialist forecast, final validation.
    assert row["usage"]["model_calls"] == 4 and row["usage"]["tool_calls"] == 4
    assert [t["kind"] for t in traces(env)].count("agent_start") == 2  # main + one specialist


def test_registry_connects_all_three_arms(env):
    registry = controller_registry(env["history"], client_factory=Script())
    assert set(registry) == {"fixed", "single_agent", "multi_agent"}


def test_team_output_cap_is_recorded_on_every_conversation(env):
    start(env)
    script = Script(
        reply(call("ask_inventory_specialist", question="Reorder?", skus=[FAST])),
        reply(call("submit_recommendation", rationale="Low cover.", actions=[ORDER])),
        reply(
            call("submit_decision", decision="proposal", explanation="Reorder.", actions=[ORDER])
        ),
    )
    assert worker(env, script).run_once()["state"] == "PROPOSED"
    starts = {
        t["conv"].split(":")[0]: t["limits"] for t in traces(env) if t["kind"] == "agent_start"
    }
    assert starts["main"]["max_output_tokens"] == 5000
    assert starts["main"]["max_model_calls"] == 6 and starts["main"]["max_input_tokens"] == 18000
    assert starts["inventory"]["output_per_call"] == 1000
