"""Local data, simulation, and approval-service commands. No paid model calls."""

import argparse
import hashlib
import json
from decimal import Decimal
from pathlib import Path

from ecommerce_agent.budget import (
    CORE_EPISODES_PER_POLICY,
    LLM_POLICY_ARMS,
    REVIEWS_PER_EPISODE,
    InvestigationLimits,
    evaluation_estimates,
)
from ecommerce_agent.config import load_config
from ecommerce_agent.contracts import StoreSnapshot
from ecommerce_agent.data import prepare_m5
from ecommerce_agent.evaluation import compare_controls
from ecommerce_agent.fixtures import FIXTURE_SEED, build_fixture, write_fixture
from ecommerce_agent.forecasting import validate_forecasts
from ecommerce_agent.simulation import Scenario, run_episode


def run_smoke(config_path: Path) -> dict:
    settings = load_config(config_path)
    snapshot = build_fixture(config=settings)
    serialized = snapshot.model_dump_json()
    restored = StoreSnapshot.model_validate_json(serialized)
    assert restored == snapshot
    assert len(snapshot.products) == len(snapshot.inventory) == 10
    assert len(snapshot.suppliers) == 2
    assert all(item.stock >= 0 for item in snapshot.inventory)
    return {
        "status": "passed",
        "scope": "phase_0_config_and_operational_snapshot",
        "products": len(snapshot.products),
        "suppliers": len(snapshot.suppliers),
        "snapshot_sha256": hashlib.sha256(serialized.encode()).hexdigest(),
        "model_calls": 0,
        "database_required": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config/development.yaml"))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("smoke", help="Validate configuration and a synthetic store round trip")
    seed_parser = commands.add_parser(
        "seed", help="Write a reproducible synthetic operational fixture"
    )
    seed_parser.add_argument("--output", type=Path, default=Path("var/fixture"))
    seed_parser.add_argument("--seed", type=int, default=FIXTURE_SEED)
    commands.add_parser("budget", help="Estimate planned model token cost, without API calls")
    setup_parser = commands.add_parser("local-setup", help="Start PostgreSQL and provision roles")
    setup_parser.add_argument("--data-dir", type=Path, default=Path("var/data/m5"))
    setup_parser.add_argument("--runtime-dir", type=Path, default=Path("var/runtime"))
    setup_parser.add_argument("--database", default="ecommerce_agent")
    commands.add_parser("local-db-stop", help="Stop local PostgreSQL without deleting its data")
    demo_parser = commands.add_parser("approval-demo", help="Run a real local approval flow")
    demo_parser.add_argument("--data-dir", type=Path, default=Path("var/data/m5"))
    demo_parser.add_argument("--output", type=Path, default=Path("var/approval-demo"))
    worker_parser = commands.add_parser("worker", help="Claim and process at most one durable job")
    worker_parser.add_argument("--kind", choices=["executor", "simulator"], required=True)
    worker_parser.add_argument("--tenant", default="store-a")
    worker_parser.add_argument("--credential-file", type=Path, required=True)
    monitor_parser = commands.add_parser(
        "monitor", help="Process pending public observations into durable alerts"
    )
    monitor_parser.add_argument("--tenant", default="store-a")
    monitor_parser.add_argument("--credential-file", type=Path, required=True)
    monitor_parser.add_argument("--data-dir", type=Path, default=Path("var/data/m5"))
    for name, text in (
        ("schedule", "Queue today's scheduled review if it is due"),
        ("investigate-worker", "Claim and run at most one investigation job"),
    ):
        job_parser = commands.add_parser(name, help=text)
        job_parser.add_argument("--tenant", default="store-a")
        job_parser.add_argument("--credential-file", type=Path, required=True)
    episode_parser = commands.add_parser(
        "episode", help="Run one persisted 28-day episode through the full application path"
    )
    episode_parser.add_argument("--data-dir", type=Path, default=Path("var/data/m5"))
    episode_parser.add_argument("--output", type=Path, default=Path("var/episodes"))
    episode_parser.add_argument(
        "--scenario", choices=["normal", "demand_surge", "supplier_delay"], default="normal"
    )
    episode_parser.add_argument("--seed", type=int, default=20261119)
    episode_parser.add_argument("--replicate", type=int, default=0)
    episode_parser.add_argument(
        "--controller", choices=["fixed", "single_agent", "multi_agent"], default="fixed"
    )
    episode_parser.add_argument("--keep-database", action="store_true")
    episode_parser.add_argument("--check-offline", action="store_true")
    data_parser = commands.add_parser(
        "prepare-data", help="Curate ten M5 products using training data"
    )
    data_parser.add_argument("--source-dir", type=Path, required=True)
    data_parser.add_argument("--output", type=Path, default=Path("var/data/m5"))
    sim_parser = commands.add_parser("simulate", help="Run a development-only store simulation")
    sim_parser.add_argument("--data-dir", type=Path, default=Path("var/data/m5"))
    sim_parser.add_argument("--output", type=Path, default=Path("var/simulation"))
    sim_parser.add_argument("--seed", type=int, default=20261119)
    sim_parser.add_argument("--replicate", type=int, default=0)
    sim_parser.add_argument(
        "--scenario", choices=["normal", "demand_surge", "supplier_delay"], default="normal"
    )
    sim_parser.add_argument(
        "--policy", choices=["no-op", "scripted", "rules", "fixed"], default="no-op"
    )
    for name, destination in (
        ("forecast-validation", "var/forecast_validation"),
        ("compare-controls", "var/controls"),
    ):
        command = commands.add_parser(name)
        command.add_argument("--data-dir", type=Path, default=Path("var/data/m5"))
        command.add_argument("--output", type=Path, default=Path(destination))
    args = parser.parse_args()
    if args.command == "smoke":
        result = run_smoke(args.config)
    elif args.command == "local-setup":
        from ecommerce_agent.local_setup import bootstrap

        setup = bootstrap(args.database, args.runtime_dir, args.data_dir)
        result = {
            "status": "ready",
            "database": setup["database"],
            "runtime_directory": setup["runtime_dir"],
            "api_environment": str(args.runtime_dir / "api.env"),
            "sessions_file": str(args.runtime_dir / "sessions.json"),
            "existing_store_state_preserved": True,
            "model_calls": 0,
        }
    elif args.command == "local-db-stop":
        from ecommerce_agent.local_database import stop_database

        stop_database()
        result = {"status": "stopped", "data_preserved": True}
    elif args.command == "approval-demo":
        from ecommerce_agent.approval_demo import run_approval_demo

        result = run_approval_demo(args.data_dir, args.output)
    elif args.command == "worker":
        from ecommerce_agent.workers import Worker

        credential = json.loads(args.credential_file.read_text())
        receipt = Worker(credential["dsn"], args.tenant, args.kind).run_once()
        result = {"status": "idle" if receipt is None else "completed", "result": receipt}
    elif args.command == "monitor":
        from ecommerce_agent.forecasting import load_history
        from ecommerce_agent.monitoring import Monitor, thresholds_record

        credential = json.loads(args.credential_file.read_text())
        history = load_history(args.data_dir, tenant_id=args.tenant)
        processed = Monitor(credential["dsn"], args.tenant, history).run_pending()
        result = {
            "status": "idle" if not processed else "processed",
            "observations": processed,
            "thresholds": thresholds_record(),
            "model_calls": 0,
        }
    elif args.command in {"schedule", "investigate-worker"}:
        from ecommerce_agent.scheduling import InvestigationWorker, Scheduler

        dsn = json.loads(args.credential_file.read_text())["dsn"]
        if args.command == "schedule":
            result = Scheduler(dsn, args.tenant).run_once()
        else:
            outcome = InvestigationWorker(dsn, args.tenant).run_once()
            result = {"status": "idle" if outcome is None else "completed", "result": outcome}
    elif args.command == "episode":
        from ecommerce_agent.episode import run_persisted_episode

        report = run_persisted_episode(
            args.data_dir,
            Scenario(
                scenario_id=f"dev-{args.scenario}-001",
                family=args.scenario,
                seed=args.seed,
                replicate=args.replicate,
            ),
            args.controller,
            output=args.output,
            config=load_config(args.config),
            keep_database=args.keep_database,
            check_offline=args.check_offline,
        )
        result = {
            key: report.get(key)
            for key in (
                "controller",
                "elapsed_seconds",
                "receipts",
                "usage",
                "model_calls",
                "alerts_opened",
                "offline_metrics_match",
                "report",
            )
        } | {
            "reviews": [(i["review_day"], i["state"]) for i in report["investigations"]],
            "core_metrics": report["metrics"]["core"],
        }
    elif args.command == "seed":
        result = write_fixture(args.output, args.seed, load_config(args.config))
    elif args.command == "prepare-data":
        try:
            result = prepare_m5(args.source_dir, args.output)
        except (ValueError, FileNotFoundError) as exc:
            parser.error(str(exc))
    elif args.command == "simulate":
        try:
            scenario = Scenario(
                scenario_id=f"dev-{args.scenario}-001",
                family=args.scenario,
                seed=args.seed,
                replicate=args.replicate,
            )
            result = run_episode(
                args.data_dir, args.output, scenario, args.policy, load_config(args.config)
            )
        except (ValueError, FileNotFoundError) as exc:
            parser.error(str(exc))
    elif args.command in {"forecast-validation", "compare-controls"}:
        try:
            if args.command == "forecast-validation":
                report = validate_forecasts(args.data_dir, args.output)
                result = {
                    "status": "completed",
                    "partition": "validation",
                    "output": str(args.output),
                    "final_test_evaluated": False,
                    "model_calls": 0,
                    "metrics": {
                        model: {k: v for k, v in values.items() if k != "per_sku"}
                        for model, values in report["metrics"].items()
                    },
                }
            else:
                result = compare_controls(
                    args.data_dir, args.output, config=load_config(args.config)
                )
        except (ValueError, FileNotFoundError) as exc:
            parser.error(str(exc))
    else:
        settings = load_config(args.config)
        if settings.model.model_id not in {"gpt-5-nano", "gpt-5-nano-2025-08-07"}:
            parser.error(
                "budget rates are verified only for GPT-5 nano; update rates before estimating"
            )
        limits = InvestigationLimits(
            input_tokens=settings.model.max_input_tokens,
            output_tokens=settings.model.max_output_tokens,
        )
        estimates = evaluation_estimates(limits=limits)
        result = {
            "model_id": settings.model.model_id,
            "pricing_model": "gpt-5-nano",
            "pricing_checked": "2026-11-18",
            "rates_usd_per_million": {"input": "0.05", "output": "0.40"},
            "monthly_allocation_usd": str(Decimal(settings.model.monthly_budget_cents) / 100),
            "llm_policy_arms": list(LLM_POLICY_ARMS),
            "core_store_episodes": CORE_EPISODES_PER_POLICY * (1 + len(LLM_POLICY_ARMS)),
            "reviews_per_episode": REVIEWS_PER_EPISODE,
            "core_investigations_per_llm_policy": (CORE_EPISODES_PER_POLICY * REVIEWS_PER_EPISODE),
            "assumptions": [
                "Uncached standard-rate tokens; course gateway rates must be verified.",
                "Output limits include all billed reasoning tokens.",
                "Includes single-agent and multi-agent arms; team caps are aggregate.",
                "Excludes development, demos, hosting, and unverified gateway charges.",
                "Sensitivity scales are planning examples, not frozen final experiments.",
                "Estimates do not enforce a spending limit or check remaining quota.",
            ],
            "experiments": {
                name: {
                    "investigations": estimate.investigations,
                    "input_tokens": estimate.input_tokens,
                    "output_tokens": estimate.output_tokens,
                    "cost_usd": str(estimate.cost_usd),
                }
                for name, estimate in estimates.items()
            },
        }
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
