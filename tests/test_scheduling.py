"""Phase 3B exit gate: automatic reviews run without a merchant, once, survive worker restarts,
and cannot approve or execute anything."""

import time
from uuid import uuid4

import psycopg
import pytest
from test_monitoring import FAST, publish, snapshot

from ecommerce_agent.contracts import CreatePurchaseOrderAction
from ecommerce_agent.monitoring import Monitor
from ecommerce_agent.oversight import Oversight
from ecommerce_agent.persistence import ServiceError, configure_store, transaction
from ecommerce_agent.scheduling import (
    InvestigationWorker,
    Outcome,
    Scheduler,
    review_blocker,
)
from ecommerce_agent.workers import Worker


def proactive(env, mode="evaluation", controller="fixed"):
    configure_store(env["admin"], "store-a", mode=mode, controller=controller)


def monitor(env):
    return Monitor(env["services"]["monitor_0"], "store-a", env["history"]).run_pending()


def scheduler(env, tenant="store-a"):
    return Scheduler(env["services"]["scheduler_0"], tenant)


def worker(env, controller=None, **kwargs):
    controllers = (
        {"fixed": controller, "single_agent": controller, "multi_agent": controller}
        if controller
        else None
    )
    return InvestigationWorker(env["services"]["agent_0"], "store-a", controllers, **kwargs)


def investigations(env):
    with transaction(env["admin"]) as conn:
        return conn.execute(
            "SELECT * FROM app.investigations WHERE tenant_id='store-a' ORDER BY created_at"
        ).fetchall()


def advance(env):
    token = env["tokens"]["merchant-a"]
    api = Oversight(env["services"]["api"])
    version = api.get_store(token, "store-a")["snapshot"]["snapshot_version"]
    api.request_advance(token, "store-a", version, str(uuid4()))
    return Worker(env["services"]["simulator_0"], "store-a", "simulator").run_once()


def test_review_is_scheduled_once_after_monitoring_without_a_merchant(env):
    proactive(env)
    publish(env, snapshot(stock=lambda sku: 5 if sku == FAST else 20, version=2))
    assert scheduler(env).run_once()["status"] == "waiting_for_monitor"
    monitor(env)
    first = scheduler(env).run_once()
    assert first == {"status": "scheduled", "run_id": "review-store-a-day1", "alerts": 1}
    assert scheduler(env).run_once()["status"] == "already_scheduled"
    rows = investigations(env)
    assert len(rows) == 1
    row = rows[0]
    assert (row["source"], row["review_day"], row["controller"], row["state"]) == (
        "scheduled",
        1,
        "fixed",
        "QUEUED",
    )
    assert [a["alert_type"] for a in row["request"]["alerts"]] == ["stockout_risk"]


def test_manual_store_never_schedules(env):
    monitor(env)
    assert scheduler(env).run_once() == {"status": "disabled", "mode": "manual"}


def test_day_cannot_advance_until_review_has_finished(env):
    proactive(env)
    Oversight(env["services"]["api"]).set_execution(env["tokens"]["merchant-a"], "store-a", True)
    assert review_blocker(env["services"]["api"], "store-a", env["tokens"]["merchant-a"]) == (
        "review_not_run"
    )
    with pytest.raises(ServiceError, match="review_not_run"):
        advance(env)
    monitor(env)
    scheduler(env).run_once()
    with pytest.raises(ServiceError, match="review_in_progress"):
        advance(env)
    result = worker(env).run_once()
    assert result["state"] == "NO_OP"
    assert advance(env)["day"] == 2
    monitor(env)
    assert scheduler(env).run_once()["status"] == "not_review_day"
    assert advance(env)["day"] == 3  # Days 2 and 3 are not review days.


def test_merchant_questions_follow_store_mode_and_mode_freezes(env):
    api, token = Oversight(env["services"]["api"]), env["tokens"]["merchant-a"]
    proactive(env, mode="evaluation")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        api.investigate(token, "store-a", "Should I discount anything?", "q1")
    proactive(env, mode="demo", controller="single_agent")
    run = api.investigate(token, "store-a", "Should I discount anything?", "q2")
    row = investigations(env)[0]
    assert (row["run_id"], row["source"], row["controller"]) == (
        run["run_id"],
        "merchant",
        "single_agent",
    )
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="frozen"):
        proactive(env, mode="evaluation")


def test_worker_crash_keeps_usage_and_traces_and_retry_limit_fails_safely(env):
    proactive(env)
    monitor(env)
    scheduler(env).run_once()

    def crashing(context):
        context.charge(model_calls=1, input_tokens=100)
        context.trace({"step": "partial"})
        raise RuntimeError("worker died mid-investigation")

    for _ in range(3):
        with pytest.raises(RuntimeError):
            worker(env, crashing).run_once()
        with transaction(env["admin"]) as conn:  # Skip the short retry backoff.
            conn.execute("UPDATE app.investigations SET ready_at=clock_timestamp()")
    row = investigations(env)[0]
    assert (row["state"], row["attempts"], row["last_error"]) == ("FAILED", 3, "RuntimeError")
    assert row["usage"] == {"model_calls": 3, "input_tokens": 300}  # Never reset by retries.
    with transaction(env["admin"]) as conn:
        traces = conn.execute(
            "SELECT sequence FROM app.thread_records WHERE run_id=%s ORDER BY sequence",
            (row["run_id"],),
        ).fetchall()
    assert [t["sequence"] for t in traces] == [1, 2, 3]
    assert worker(env).run_once() is None


def test_expired_lease_is_reclaimed_and_old_worker_is_fenced(env):
    proactive(env)
    monitor(env)
    scheduler(env).run_once()
    stuck = worker(env)
    claim = stuck.claim()  # Worker dies here without releasing.
    with transaction(env["admin"]) as conn:
        conn.execute("UPDATE app.investigations SET lease_expires_at=clock_timestamp()")
    result = worker(env).run_once()
    assert result["state"] == "NO_OP" and investigations(env)[0]["attempts"] == 2
    with pytest.raises(ServiceError, match="lease"):
        stuck.execute(**claim)


def test_proposal_survives_crash_after_submission_without_duplicates(env):
    proactive(env)
    monitor(env)
    scheduler(env).run_once()

    def propose(context):
        return Outcome(
            "proposal",
            "Reorder the fast-supplier item.",
            (CreatePurchaseOrderAction(sku=FAST, quantity=5),),
            ("alert:test",),
        )

    def crash(stage):
        if stage == "after_proposal":
            raise RuntimeError(stage)

    first = worker(env, propose, failpoint=crash)
    with pytest.raises(RuntimeError):
        first.run_once()
    with transaction(env["admin"]) as conn:
        conn.execute("UPDATE app.investigations SET ready_at=clock_timestamp()")
    result = worker(env, propose).run_once()
    assert result["state"] == "PROPOSED" and result["recovered"] is True
    with transaction(env["admin"]) as conn:
        proposals = conn.execute("SELECT status FROM app.proposals").fetchall()
        approvals = conn.execute("SELECT 1 FROM app.approvals").fetchall()
    assert [p["status"] for p in proposals] == ["PENDING"] and approvals == []


def test_wall_clock_budget_ends_in_timeout(env):
    proactive(env)
    monitor(env)
    scheduler(env).run_once()

    def slow(context):
        time.sleep(1.2)
        context.check_deadline()
        raise AssertionError("unreachable")

    result = worker(env, slow, lease_seconds=5, wall_seconds=1).run_once()
    assert result["state"] == "TIMED_OUT"
    assert "budget exhausted" in investigations(env)[0]["outcome"]["explanation"]


def test_scheduler_role_cannot_approve_execute_or_cross_tenants(env):
    proactive(env)
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="store access denied"):
        scheduler(env, "store-b").run_once()
    for query in (
        "SELECT * FROM evaluator.sandbox_state",
        "UPDATE app.stores SET snapshot=snapshot",
        "INSERT INTO app.proposals SELECT * FROM app.proposals",
        "INSERT INTO app.approvals SELECT * FROM app.approvals",
        "INSERT INTO app.execution_jobs SELECT * FROM app.execution_jobs",
        "UPDATE app.investigations SET state='NO_OP'",
    ):
        with (
            pytest.raises(psycopg.errors.InsufficientPrivilege),
            transaction(env["services"]["scheduler_0"]) as conn,
        ):
            conn.execute(query)
    for source, day in (("scheduled", 4), ("merchant", None)):  # Wrong day; not a merchant.
        with (
            pytest.raises(psycopg.errors.InsufficientPrivilege),
            transaction(env["services"]["scheduler_0"]) as conn,
        ):
            conn.execute(
                "INSERT INTO app.investigations(tenant_id,run_id,source,review_day) "
                "VALUES('store-a',%s,%s,%s)",
                (str(uuid4()), source, day),
            )
