"""Phase 3C exit gate: the fixed workflow completes a persisted 28-day episode through the real
application path, matches the offline harness, and survives worker failure without changing
results. No LLM calls and no privileged controller writes."""

from datetime import date
from pathlib import Path

import pytest
from test_monitoring import ORIGIN, base_history, snapshot

from ecommerce_agent.config import load_config
from ecommerce_agent.controllers import fixed_controller
from ecommerce_agent.controls import DeterministicControl
from ecommerce_agent.episode import EpisodeRunner, run_persisted_episode
from ecommerce_agent.simulation import Initialization, Scenario, Simulator, World

M5 = Path("var/data/m5")


def synthetic(family="normal"):
    snap = snapshot(stock=lambda sku: 4 if sku.endswith(("1", "4")) else 12)
    snap = snap.model_copy(update={"tenant_id": "synthetic-store"})
    world = World(
        Scenario(scenario_id=f"test-{family}", family=family),
        ORIGIN,
        {p.sku: (1.0, 2.0, 1.0, 3.0, 1.0, 2.0, 1.0) for p in snap.products},
        {p.sku: -1.2 for p in snap.products},
    )
    history = base_history([p.sku for p in snap.products], rate=2)
    return Initialization(snap, world), history


def run(postgres_server, tmp_path, controllers=None, family="normal"):
    initial, history = synthetic(family)
    runner = EpisodeRunner(
        initial,
        history,
        load_config(),
        controllers or {"fixed": fixed_controller(history)},
        "fixed",
        server_dsn=postgres_server,
        runtime_dir=tmp_path,
    )
    runner.setup()
    try:
        return runner.run()
    finally:
        runner.teardown()


def offline(family="normal"):
    initial, history = synthetic(family)
    config = load_config()
    engine = Simulator(initial, config)
    control = DeterministicControl(
        "fixed",
        history.__class__(initial.snapshot.tenant_id, history.records, history.source_id),
        config,
    )
    while not engine.complete:
        visible = engine.observe()
        actions = control.decide(visible) if visible.day in config.simulation.review_days else ()
        engine.step(actions, expected_snapshot_version=visible.snapshot_version)
        rows = engine.sales_observations[-len(visible.products) :]
        control.record_completed_sales(rows, date.fromisoformat(rows[0]["date"]))
    return engine.metrics(), control.decisions


@pytest.mark.parametrize("family", ["normal", "demand_surge"])
def test_persisted_episode_matches_offline_fixed_workflow(postgres_server, tmp_path, family):
    report = run(postgres_server, tmp_path, family=family)
    metrics, decisions = offline(family)
    assert report["metrics"] == metrics
    assert [i["review_day"] for i in report["investigations"]] == [1, 4, 7, 10]
    expected = ["PROPOSED" if d["actions"] else "NO_OP" for d in decisions]
    assert [i["state"] for i in report["investigations"]] == expected
    assert report["model_calls"] == 0
    assert report["usage"] == {"tool_calls": 16, "compare_calls": 4}
    advances = [e for e in report["events"] if e["step"] == "advance"]
    assert len(advances) == 28 and advances[-1]["complete"] is True


def test_every_store_change_traces_to_an_approved_proposal_and_receipt(postgres_server, tmp_path):
    report = run(postgres_server, tmp_path)
    proposed = [i for i in report["investigations"] if i["state"] == "PROPOSED"]
    assert proposed, "fixture should produce at least one proposal"
    assert [p["status"] for p in report["proposals"]] == ["EXECUTED"] * len(proposed)
    assert report["receipts"] == len(proposed)
    approvals = [e for e in report["events"] if e["step"] == "approval"]
    assert [a["status"] for a in approvals] == ["APPROVED"] * len(proposed)


def test_worker_crash_mid_review_is_retried_without_changing_results(postgres_server, tmp_path):
    clean = run(postgres_server, tmp_path / "clean")
    initial, history = synthetic()
    fixed, crashed = fixed_controller(history), []

    def flaky(context):
        if context.snapshot.day == 4 and not crashed:
            crashed.append(True)
            context.charge(tool_calls=1)  # Work done before the crash still counts.
            raise RuntimeError("worker died")
        return fixed(context)

    faulty = run(postgres_server, tmp_path / "faulty", {"fixed": flaky})
    assert faulty["metrics"] == clean["metrics"]
    day4 = next(i for i in faulty["investigations"] if i["review_day"] == 4)
    assert day4["attempts"] == 2 and day4["usage"]["tool_calls"] == 5
    assert any(e["step"] == "investigation_retry" for e in faulty["events"])


@pytest.mark.skipif(not (M5 / "manifest.json").exists(), reason="prepared M5 data not present")
@pytest.mark.parametrize("family", ["normal", "demand_surge", "supplier_delay"])
def test_m5_development_episodes_match_offline_harness(tmp_path, family):
    report = run_persisted_episode(
        M5,
        Scenario(scenario_id=f"dev-{family}-001", family=family),
        output=tmp_path,
        check_offline=True,
    )
    assert report["offline_metrics_match"] is True
