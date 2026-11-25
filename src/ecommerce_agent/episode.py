"""Phase 3C: one complete persisted store episode through the real application path.

Each episode gets a fresh database. A single driver performs every step in order, each under its
own restricted credential: monitor -> scheduler -> investigation worker -> mock merchant approval
over the real API service -> executor -> day advance. The mock merchant approves every valid
proposal; escalations count as no change. Results are exported to JSON and the database is
dropped unless kept for inspection.
"""

from __future__ import annotations

import json
import time
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

from ecommerce_agent.config import ProjectConfig, load_config
from ecommerce_agent.durable_simulator import checkpoint, restore
from ecommerce_agent.forecasting import SalesHistory, load_history
from ecommerce_agent.local_database import start_database
from ecommerce_agent.monitoring import Monitor, thresholds_record
from ecommerce_agent.oversight import Oversight
from ecommerce_agent.persistence import (
    configure_store,
    create_database,
    issue_session,
    migrate,
    provision_runtime_roles,
    seed_store,
    transaction,
)
from ecommerce_agent.scheduling import InvestigationWorker, Scheduler
from ecommerce_agent.simulation import Initialization, Scenario, Simulator, initialize_store
from ecommerce_agent.workers import Worker

MERCHANT = "evaluation-merchant"


def seed_merchant(admin_dsn, tenant):
    with transaction(admin_dsn) as conn:
        conn.execute(
            "INSERT INTO authn.users(user_id,display_name) VALUES(%s,%s) ON CONFLICT DO NOTHING",
            (MERCHANT, "Automated evaluation merchant"),
        )
        conn.execute(
            "INSERT INTO authn.memberships VALUES(%s,%s,'admin') ON CONFLICT DO NOTHING",
            (tenant, MERCHANT),
        )
    return issue_session(admin_dsn, MERCHANT)


class EpisodeRunner:
    def __init__(
        self,
        initial: Initialization,
        base_history: SalesHistory,
        config: ProjectConfig,
        controllers: dict,
        controller: str,
        *,
        server_dsn=None,
        runtime_dir: Path,
    ):
        # Keep the scenario's tenant ID: the planner keys its rollout draws on it, so matched
        # controllers and the offline harness see identical planning randomness.
        self.initial = initial
        self.tenant = initial.snapshot.tenant_id
        self.base_history = SalesHistory(self.tenant, base_history.records, base_history.source_id)
        self.config, self.controllers, self.controller = config, controllers, controller
        self.server_dsn = server_dsn or start_database()
        self.runtime_dir = runtime_dir
        self.database = "ec_ep_" + uuid4().hex[:12]
        self.events = []

    def setup(self):
        self.admin = create_database(self.server_dsn, self.database)
        migrate(self.admin)
        engine = Simulator(self.initial, self.config)
        seed_store(
            self.admin,
            replace(self.initial, snapshot=engine.observe()),
            self.config,
            checkpoint(engine),
        )
        configure_store(self.admin, self.tenant, mode="evaluation", controller=self.controller)
        self.services = provision_runtime_roles(
            self.admin, [self.tenant], self.runtime_dir / f"{self.database}.json"
        )
        self.token = seed_merchant(self.admin, self.tenant)
        self.api = Oversight(self.services["api"])
        self.api.set_execution(self.token, self.tenant, True)

    def teardown(self, keep=False):
        if keep:
            return
        with psycopg.connect(self.server_dsn, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(self.database)
                )
            )
            for dsn in self.services.values():
                conn.execute(
                    sql.SQL("DROP ROLE IF EXISTS {}").format(
                        sql.Identifier(conninfo_to_dict(dsn)["user"])
                    )
                )
        (self.runtime_dir / f"{self.database}.json").unlink(missing_ok=True)

    def _log(self, day, step, **data):
        self.events.append({"day": day, "step": step} | data)

    def _investigate(self, day):
        worker = InvestigationWorker(self.services["agent_0"], self.tenant, self.controllers)
        while True:
            try:
                result = worker.run_once()
            except Exception as exc:  # Durable retry: a fresh worker reclaims the job.
                self._log(day, "investigation_retry", error=type(exc).__name__)
                with transaction(self.admin) as conn:  # Skip backoff in automated runs.
                    conn.execute("UPDATE app.investigations SET ready_at=clock_timestamp()")
                worker = InvestigationWorker(
                    self.services["agent_0"], self.tenant, self.controllers
                )
                continue
            if result is None:
                return
            self._log(day, "investigation", state=result["state"], run_id=result["run_id"])
            if result["state"] == "PROPOSED":
                self._approve_and_execute(day, result)

    def _approve_and_execute(self, day, result):
        decision = self.api.decide(
            self.token, self.tenant, result["proposal_id"], result["payload_hash"], str(uuid4())
        )
        self._log(day, "approval", proposal_id=result["proposal_id"], status=decision["status"])
        executor = Worker(self.services["executor_0"], self.tenant)
        receipt = executor.run_once()
        self._log(
            day,
            "execution",
            proposal_id=result["proposal_id"],
            executed=receipt is not None,
            actions=len(receipt["effects"]["new_orders"]) if receipt else 0,
        )

    def _advance(self, day):
        version = self.api.get_store(self.token, self.tenant)["snapshot"]["snapshot_version"]
        self.api.request_advance(self.token, self.tenant, version, f"advance-{day}")
        return Worker(self.services["simulator_0"], self.tenant, "simulator").run_once()

    def run(self):
        started = time.monotonic()
        monitor = Monitor(self.services["monitor_0"], self.tenant, self.base_history)
        scheduler = Scheduler(self.services["scheduler_0"], self.tenant)
        total = self.config.simulation.episode_days + self.config.simulation.continuation_days
        for day in range(1, total + 1):
            processed = monitor.run_pending()
            self._log(day, "monitor", observations=len(processed))
            schedule = scheduler.run_once()
            if schedule["status"] == "scheduled":
                self._log(day, "scheduled", alerts=schedule["alerts"])
                self._investigate(day)
            result = self._advance(day)
            self._log(day, "advance", day_after=result["day"], complete=result["episode_complete"])
        monitor.run_pending()
        return self.report(time.monotonic() - started)

    def report(self, elapsed):
        with transaction(self.admin) as conn:
            private = conn.execute(
                "SELECT checkpoint FROM evaluator.sandbox_state WHERE tenant_id=%s", (self.tenant,)
            ).fetchone()["checkpoint"]
            investigations = conn.execute(
                "SELECT run_id,source,review_day,controller,state,attempts,outcome,usage "
                "FROM app.investigations WHERE tenant_id=%s ORDER BY review_day",
                (self.tenant,),
            ).fetchall()
            proposals = conn.execute(
                "SELECT run_id,status,payload->'actions' AS actions FROM app.proposals "
                "WHERE tenant_id=%s ORDER BY created_at",
                (self.tenant,),
            ).fetchall()
            receipts = conn.execute(
                "SELECT count(*) AS n FROM app.executions WHERE tenant_id=%s", (self.tenant,)
            ).fetchone()["n"]
            alerts = conn.execute(
                "SELECT alert_type,count(*) AS n FROM app.alerts WHERE tenant_id=%s "
                "GROUP BY alert_type ORDER BY alert_type",
                (self.tenant,),
            ).fetchall()
        engine = restore(private)
        if not engine.complete:
            raise RuntimeError("episode did not complete")
        usage = {}
        for row in investigations:
            for key, value in row["usage"].items():
                usage[key] = usage.get(key, 0) + value
        return {
            "controller": self.controller,
            "scenario": vars(self.initial.world.scenario),
            "database": self.database,
            "elapsed_seconds": round(elapsed, 2),
            "metrics": engine.metrics(),
            "investigations": [
                {k: row[k] for k in ("review_day", "state", "attempts", "usage")}
                | {"outcome_kind": (row["outcome"] or {}).get("kind")}
                for row in investigations
            ],
            "proposals": [dict(p) for p in proposals],
            "receipts": receipts,
            "alerts_opened": {a["alert_type"]: a["n"] for a in alerts},
            "usage": usage,
            "model_calls": usage.get("model_calls", 0),
            "monitor": thresholds_record(),
            "events": self.events,
        }


def run_persisted_episode(
    data_dir: Path,
    scenario: Scenario,
    controller="fixed",
    *,
    output: Path = Path("var/episodes"),
    config=None,
    keep_database=False,
    check_offline=False,
):
    from ecommerce_agent.controllers import controller_registry

    config = config or load_config()
    history = load_history(data_dir, tenant_id="m5")
    runner = EpisodeRunner(
        initialize_store(data_dir, scenario, config),
        history,
        config,
        controller_registry(history),
        controller,
        runtime_dir=output / "runtime",
    )
    runner.setup()
    try:
        report = runner.run()
    finally:
        runner.teardown(keep_database)
    if check_offline:
        if controller != "fixed":
            raise ValueError("the offline harness exists only for the fixed controller")
        from ecommerce_agent.simulation import run_episode

        offline = run_episode(
            data_dir, output / "offline" / runner.database, scenario, "fixed", config
        )
        report["offline_metrics_match"] = offline["metrics"] == report["metrics"]
    name = f"{scenario.scenario_id}-r{scenario.replicate}-{controller}"
    output.mkdir(parents=True, exist_ok=True)
    (output / f"{name}.json").write_text(json.dumps(report, indent=2, sort_keys=True, default=str))
    return report | {"report": str(output / f"{name}.json")}
