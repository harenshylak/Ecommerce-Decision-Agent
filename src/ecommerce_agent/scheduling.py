"""Phase 3B: automatic review scheduling and leased investigation workers.

The scheduler runs under its own tenant-bound database role. It can queue at most one review per
store and review day, after the monitor has processed the current snapshot. It cannot propose,
approve, or execute. The investigation worker runs under the agent role and calls the store's
assigned controller; proposals still go through the existing approval and executor path.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from ecommerce_agent.config import ProjectConfig
from ecommerce_agent.contracts import StoreSnapshot
from ecommerce_agent.oversight import Oversight, audit
from ecommerce_agent.persistence import ServiceError, transaction

TERMINAL = ("PROPOSED", "NO_OP", "ESCALATED", "TIMED_OUT", "FAILED")
USAGE_KEYS = (
    "model_calls",
    "input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "tool_calls",
    "compare_calls",
    "invalid_tool_calls",
    "model_errors",
    "uncertain_calls",
    "delegations",
)


def review_blocker(dsn, tenant, token=None):
    with transaction(dsn, token) as conn:
        return conn.execute("SELECT app.review_blocker(%s) AS reason", (tenant,)).fetchone()[
            "reason"
        ]


class Scheduler:
    def __init__(self, dsn, tenant):
        self.dsn, self.tenant = dsn, tenant

    def run_once(self):
        """Queue today's review if due. Safe to call any number of times."""
        with transaction(self.dsn) as conn:
            store = conn.execute("SELECT * FROM authn.lock_store(%s)", (self.tenant,)).fetchone()
            if store is None:
                raise ServiceError("forbidden", "store access denied", 403)
            snapshot = StoreSnapshot.model_validate(store["snapshot"])
            if store["mode"] == "manual":
                return {"status": "disabled", "mode": "manual"}
            config = conn.execute(
                "SELECT config FROM app.policy_versions WHERE tenant_id=%s AND version=%s",
                (self.tenant, store["policy_version"]),
            ).fetchone()["config"]
            if snapshot.day not in config["simulation"]["review_days"]:
                return {"status": "not_review_day", "day": snapshot.day}
            cursor = conn.execute(
                "SELECT last_snapshot_version FROM app.monitor_cursors WHERE tenant_id=%s",
                (self.tenant,),
            ).fetchone()
            if cursor is None or cursor["last_snapshot_version"] < snapshot.snapshot_version:
                return {"status": "waiting_for_monitor", "day": snapshot.day}
            existing = conn.execute(
                "SELECT run_id,state FROM app.investigations WHERE tenant_id=%s "
                "AND source='scheduled' AND review_day=%s",
                (self.tenant, snapshot.day),
            ).fetchone()
            if existing:
                return {"status": "already_scheduled"} | dict(existing)
            alerts = conn.execute(
                "SELECT alert_id,alert_type,sku,order_id,severity,evidence FROM app.alerts "
                "WHERE tenant_id=%s AND status='OPEN' ORDER BY alert_type,sku",
                (self.tenant,),
            ).fetchall()
            run_id = f"review-{self.tenant}-day{snapshot.day}"
            request = {
                "objective": "Scheduled review: decide whether to reorder, change prices, "
                "or make no change for the current day.",
                "review_day": snapshot.day,
                "snapshot_version": snapshot.snapshot_version,
                "alerts": [a | {"alert_id": str(a["alert_id"])} for a in alerts],
            }
            conn.execute(
                "INSERT INTO app.investigations(tenant_id,run_id,request,source,review_day) "
                "VALUES(%s,%s,%s,'scheduled',%s) ON CONFLICT DO NOTHING",
                (self.tenant, run_id, Jsonb(request), snapshot.day),
            )
            actor = conn.execute("SELECT session_user AS name").fetchone()["name"]
            audit(
                conn,
                self.tenant,
                "investigation_scheduled",
                actor,
                {"run_id": run_id, "review_day": snapshot.day, "alerts": len(alerts)},
            )
            return {"status": "scheduled", "run_id": run_id, "alerts": len(alerts)}


@dataclass(frozen=True)
class Outcome:
    kind: str  # proposal | no_op | escalation
    explanation: str
    actions: tuple = ()
    evidence_ids: tuple = ()

    def __post_init__(self):
        if self.kind not in {"proposal", "no_op", "escalation"}:
            raise ValueError("unknown outcome")
        if (self.kind == "proposal") != bool(self.actions):
            raise ValueError("only a proposal carries actions")


class InvestigationTimeout(Exception):
    pass


@dataclass
class InvestigationContext:
    """What a controller may see and do. Usage and traces persist immediately."""

    tenant: str
    run_id: str
    source: str
    controller: str
    request: dict
    snapshot: StoreSnapshot
    calendar_origin: object
    config: ProjectConfig
    observed_sales: list
    usage: dict
    deadline: float
    worker: InvestigationWorker = field(repr=False)
    lease_owner: str = field(repr=False, default="")

    def charge(self, **delta):
        unknown = set(delta) - set(USAGE_KEYS)
        if unknown:
            raise ValueError(f"unknown usage keys {sorted(unknown)}")
        self.usage = self.worker._charge(self.run_id, self.lease_owner, delta)
        return self.usage

    def trace(self, data):
        self.worker._trace(self.run_id, self.lease_owner, data)

    def traces(self):
        """Every step this investigation saved so far, across attempts, in order."""
        return self.worker._traces(self.run_id, self.lease_owner)

    def check_deadline(self):
        if time.monotonic() > self.deadline:
            raise InvestigationTimeout("investigation wall-clock budget exhausted")


def noop_controller(context: InvestigationContext) -> Outcome:
    """Placeholder until Phase 3C/3D controllers are connected."""
    context.trace({"step": "placeholder", "alerts": len(context.request.get("alerts", []))})
    return Outcome("no_op", "Placeholder controller: no decision logic connected yet.")


class InvestigationWorker:
    def __init__(
        self,
        dsn,
        tenant,
        controllers=None,
        *,
        lease_seconds=150,
        wall_seconds=90,
        failpoint=None,
    ):
        if not 1 <= lease_seconds <= 600 or wall_seconds >= lease_seconds:
            raise ValueError("lease must exceed the wall-clock budget")
        self.dsn, self.tenant = dsn, tenant
        self.controllers = controllers or {
            "fixed": noop_controller,
            "single_agent": noop_controller,
            "multi_agent": noop_controller,
        }
        self.lease_seconds, self.wall_seconds, self.failpoint = (
            lease_seconds,
            wall_seconds,
            failpoint,
        )

    def claim(self):
        owner = uuid4()
        with transaction(self.dsn) as conn:
            conn.execute(
                "UPDATE app.investigations SET state='FAILED',last_error='retry_limit',"
                "lease_owner=NULL,lease_expires_at=NULL,updated_at=clock_timestamp() "
                "WHERE tenant_id=%s AND attempts>=max_attempts AND (state='QUEUED' OR "
                "(state='LEASED' AND lease_expires_at<clock_timestamp()))",
                (self.tenant,),
            )
            row = conn.execute(
                "SELECT run_id FROM app.investigations WHERE tenant_id=%s "
                "AND attempts<max_attempts AND ready_at<=clock_timestamp() AND (state='QUEUED' "
                "OR (state='LEASED' AND lease_expires_at<clock_timestamp())) "
                "ORDER BY ready_at,run_id FOR UPDATE SKIP LOCKED LIMIT 1",
                (self.tenant,),
            ).fetchone()
            if not row:
                return None
            conn.execute(
                "UPDATE app.investigations SET state='LEASED',lease_owner=%s,"
                "lease_expires_at=clock_timestamp()+(%s * interval '1 second'),"
                "heartbeat_at=clock_timestamp(),attempts=attempts+1,updated_at=clock_timestamp() "
                "WHERE tenant_id=%s AND run_id=%s",
                (owner, self.lease_seconds, self.tenant, row["run_id"]),
            )
            return {"run_id": row["run_id"], "lease_owner": str(owner)}

    def _owned(self, conn, run_id, lease_owner):
        row = conn.execute(
            "SELECT * FROM app.investigations WHERE tenant_id=%s AND run_id=%s FOR UPDATE",
            (self.tenant, run_id),
        ).fetchone()
        if (
            not row
            or row["state"] != "LEASED"
            or str(row["lease_owner"]) != str(lease_owner)
            or row["lease_expires_at"] is None
        ):
            raise ServiceError("lease_lost", "investigation lease is no longer held")
        return row

    def _charge(self, run_id, lease_owner, delta):
        with transaction(self.dsn) as conn:
            row = self._owned(conn, run_id, lease_owner)
            usage = dict(row["usage"])
            for key, value in delta.items():
                if type(value) is not int or value < 0:
                    raise ValueError("usage deltas must be nonnegative integers")
                usage[key] = usage.get(key, 0) + value
            conn.execute(
                "UPDATE app.investigations SET usage=%s,updated_at=clock_timestamp() "
                "WHERE tenant_id=%s AND run_id=%s",
                (Jsonb(usage), self.tenant, run_id),
            )
            return usage

    def _trace(self, run_id, lease_owner, data):
        with transaction(self.dsn) as conn:
            self._owned(conn, run_id, lease_owner)
            sequence = conn.execute(
                "SELECT COALESCE(max(sequence),0)+1 AS n FROM app.thread_records "
                "WHERE tenant_id=%s AND run_id=%s AND kind='trace'",
                (self.tenant, run_id),
            ).fetchone()["n"]
            conn.execute(
                "INSERT INTO app.thread_records VALUES(%s,%s,%s,'trace',%s)",
                (self.tenant, run_id, sequence, Jsonb(data)),
            )

    def _traces(self, run_id, lease_owner):
        with transaction(self.dsn) as conn:
            self._owned(conn, run_id, lease_owner)
            return [
                r["data"]
                for r in conn.execute(
                    "SELECT data FROM app.thread_records WHERE tenant_id=%s AND run_id=%s "
                    "AND kind='trace' ORDER BY sequence",
                    (self.tenant, run_id),
                ).fetchall()
            ]

    def _context(self, run_id, lease_owner):
        with transaction(self.dsn) as conn:
            row = self._owned(conn, run_id, lease_owner)
            store = conn.execute(
                "SELECT s.snapshot,s.calendar_origin,p.config FROM app.stores s "
                "JOIN app.policy_versions p ON p.tenant_id=s.tenant_id "
                "AND p.version=s.policy_version WHERE s.tenant_id=%s",
                (self.tenant,),
            ).fetchone()
            snapshot = StoreSnapshot.model_validate(store["snapshot"])
            sales = conn.execute(
                "SELECT day,sku,date,sales_units,price_cents,ended_without_stock "
                "FROM app.observed_sales WHERE tenant_id=%s AND day<%s ORDER BY day,sku",
                (self.tenant, snapshot.day),
            ).fetchall()
            proposal = conn.execute(
                "SELECT proposal_id,payload_hash FROM app.proposals WHERE tenant_id=%s "
                "AND run_id=%s",
                (self.tenant, run_id),
            ).fetchone()
        context = InvestigationContext(
            tenant=self.tenant,
            run_id=run_id,
            source=row["source"],
            controller=row["controller"],
            request=row["request"],
            snapshot=snapshot,
            calendar_origin=store["calendar_origin"],
            config=ProjectConfig.model_validate(store["config"]),
            observed_sales=sales,
            usage=dict(row["usage"]),
            deadline=time.monotonic() + self.wall_seconds,
            worker=self,
            lease_owner=lease_owner,
        )
        return context, row, proposal

    def _finish(self, run_id, lease_owner, state, outcome, error=None):
        with transaction(self.dsn) as conn:
            self._owned(conn, run_id, lease_owner)
            conn.execute(
                "UPDATE app.investigations SET state=%s,outcome=%s,last_error=%s,"
                "lease_owner=NULL,lease_expires_at=NULL,updated_at=clock_timestamp() "
                "WHERE tenant_id=%s AND run_id=%s",
                (state, Jsonb(outcome), error, self.tenant, run_id),
            )
            actor = conn.execute("SELECT session_user AS name").fetchone()["name"]
            audit(conn, self.tenant, "investigation_finished", actor, {"run_id": run_id} | outcome)
        return {"run_id": run_id, "state": state} | outcome

    def execute(self, run_id, lease_owner):
        context, row, existing = self._context(run_id, lease_owner)
        if existing:  # A previous attempt submitted the proposal, then crashed. Do not redo work.
            return self._finish(
                run_id,
                lease_owner,
                "PROPOSED",
                {
                    "kind": "proposal",
                    "proposal_id": str(existing["proposal_id"]),
                    "payload_hash": existing["payload_hash"],
                    "recovered": True,
                },
            )
        controller = self.controllers[row["controller"]]
        try:
            outcome = controller(context)
            context.check_deadline()
        except InvestigationTimeout as exc:
            return self._finish(
                run_id, lease_owner, "TIMED_OUT", {"kind": "timeout", "explanation": str(exc)}
            )
        if self.failpoint:
            self.failpoint("before_outcome")
        record = {
            "kind": outcome.kind,
            "explanation": outcome.explanation,
            "evidence_ids": list(outcome.evidence_ids),
            "usage": context.usage,
            "snapshot_version": context.snapshot.snapshot_version,
        }
        if outcome.kind == "no_op":
            return self._finish(run_id, lease_owner, "NO_OP", record)
        if outcome.kind == "escalation":
            return self._finish(run_id, lease_owner, "ESCALATED", record)
        proposal = Oversight(self.dsn).propose(
            self.tenant,
            run_id,
            outcome.actions,
            outcome.evidence_ids,
            outcome.explanation,
            f"{run_id}:proposal",
            expected_snapshot_version=context.snapshot.snapshot_version,
            expected_policy_version=context.snapshot.policy_version,
        )
        if self.failpoint:
            self.failpoint("after_proposal")
        return self._finish(run_id, lease_owner, "PROPOSED", record | proposal)

    def release(self, run_id, lease_owner, error):
        code = error.code if isinstance(error, ServiceError) else type(error).__name__
        with transaction(self.dsn) as conn:
            conn.execute(
                "UPDATE app.investigations SET state=CASE WHEN attempts>=max_attempts "
                "THEN 'FAILED' ELSE 'QUEUED' END,last_error=%s,lease_owner=NULL,"
                "lease_expires_at=NULL,ready_at=clock_timestamp()+(interval '1 second'),"
                "updated_at=clock_timestamp() "
                "WHERE tenant_id=%s AND run_id=%s AND state='LEASED' AND lease_owner=%s",
                (code, self.tenant, run_id, UUID(str(lease_owner))),
            )

    def run_once(self):
        claim = self.claim()
        if not claim:
            return None
        try:
            return self.execute(**claim)
        except Exception as exc:
            self.release(**claim, error=exc)
            raise
