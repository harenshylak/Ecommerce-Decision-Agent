"""Authenticated proposal/approval operations, with database-backed idempotency."""

import hmac
from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from psycopg.types.json import Jsonb

from ecommerce_agent.config import ProjectConfig
from ecommerce_agent.contracts import ProposalPayload, StoreSnapshot, payload_hash
from ecommerce_agent.persistence import ServiceError, digest, identity, require_member, transaction
from ecommerce_agent.validation import normalize_actions, validate_plan


def audit(conn, tenant, kind, actor, data):
    conn.execute(
        (
            "INSERT INTO app.audit_events(tenant_id,event_id,kind,ac"
            "tor_id,data) VALUES(%s,%s,%s,%s,%s)"
        ),
        (tenant, uuid4(), kind, actor, Jsonb(data)),
    )


def locked_store(conn, tenant):
    result = conn.execute("SELECT * FROM authn.lock_store(%s)", (tenant,)).fetchone()
    if result is None:
        raise ServiceError("forbidden", "store access denied", 403)
    return result


def policy_config(conn, store):
    record = conn.execute(
        "SELECT config FROM app.policy_versions WHERE tenant_id=%s AND version=%s",
        (store["tenant_id"], store["policy_version"]),
    ).fetchone()
    if record is None:
        raise ServiceError("policy_unavailable", "current policy unavailable", 503)
    return ProjectConfig.model_validate(record["config"])


def idempotent(conn, tenant, actor, operation, key, request, handler):
    if not isinstance(key, str) or not 1 <= len(key) <= 128:
        raise ServiceError("idempotency_key", "a bounded idempotency key is required", 422)
    scope = digest([tenant, actor, operation, key])
    lock = int(scope[:16], 16) % (2**63 - 1)
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (lock,))
    request_hash = digest(request)
    previous = conn.execute(
        (
            "SELECT request_hash,response FROM app.idempotency WHERE"
            " tenant_id=%s AND actor_id=%s AND operation=%s AND key="
            "%s"
        ),
        (tenant, actor, operation, key),
    ).fetchone()
    if previous:
        if not hmac.compare_digest(previous["request_hash"], request_hash):
            raise ServiceError(
                "idempotency_conflict", "idempotency key reused with different input"
            )
        return previous["response"]
    response = handler()
    conn.execute(
        "INSERT INTO app.idempotency VALUES(%s,%s,%s,%s,%s,%s)",
        (tenant, actor, operation, key, request_hash, Jsonb(response)),
    )
    return response


def review_gate(conn, tenant):
    """Proactive stores may not advance while today's review is missing or unfinished."""
    reason = conn.execute("SELECT app.review_blocker(%s) AS reason", (tenant,)).fetchone()
    if reason["reason"]:
        raise ServiceError("review_pending", reason["reason"])


def check_payload(payload, snapshot, config):
    if payload.tenant_id != snapshot.tenant_id:
        raise ServiceError("forbidden", "proposal scope mismatch", 403)
    if datetime.now(UTC) >= payload.expires_at:
        raise ServiceError("expired", "proposal expired")
    if payload.simulated_day != snapshot.day:
        raise ServiceError("stale", "simulated day changed")
    check = validate_plan(
        snapshot,
        payload.actions,
        config,
        expected_snapshot_version=payload.snapshot_version,
        expected_policy_version=payload.policy_version,
    )
    if not check["feasible"]:
        raise ServiceError("stale", "; ".join(v["message"] for v in check["violations"]))


class Oversight:
    def __init__(self, dsn):
        self.dsn = dsn

    def me(self, token):
        with transaction(self.dsn, token) as conn:
            actor = identity(conn)
            memberships = conn.execute("SELECT * FROM authn.my_memberships()").fetchall()
            return {"user_id": actor["user_id"], "memberships": memberships}

    def get_store(self, token, tenant):
        with transaction(self.dsn, token) as conn:
            require_member(conn, tenant)
            row = conn.execute("SELECT * FROM app.stores WHERE tenant_id=%s", (tenant,)).fetchone()
            if not row:
                raise ServiceError("forbidden", "store access denied", 403)
            snapshot = StoreSnapshot.model_validate(row["snapshot"])
            return {
                "snapshot": snapshot.model_dump(mode="json"),
                "execution_enabled": row["execution_enabled"],
                "as_of_date": str(row["calendar_origin"] + timedelta(days=snapshot.day - 1)),
            }

    def investigate(self, token, tenant, question, key):
        if not isinstance(question, str) or not 1 <= len(question) <= 2000:
            raise ServiceError("question", "question must have 1-2000 characters", 422)
        with transaction(self.dsn, token) as conn:
            actor = require_member(conn, tenant, True)

            def create():
                run_id = str(uuid4())
                conn.execute(
                    "INSERT INTO app.investigations(tenant_id,run_id,request) VALUES(%s,%s,%s)",
                    (tenant, run_id, Jsonb({"question": question})),
                )
                audit(conn, tenant, "investigation_created", actor["user_id"], {"run_id": run_id})
                return {"run_id": run_id, "state": "QUEUED"}

            return idempotent(
                conn, tenant, actor["user_id"], "investigate", key, {"question": question}, create
            )

    def observed_sales(self, token, tenant, lookback_days=56):
        if type(lookback_days) is not int or not 1 <= lookback_days <= 365:
            raise ServiceError("lookback", "lookback must be 1-365 days", 422)
        with transaction(self.dsn, token) as conn:
            require_member(conn, tenant)
            store = conn.execute(
                "SELECT snapshot FROM app.stores WHERE tenant_id=%s", (tenant,)
            ).fetchone()
            if not store:
                raise ServiceError("forbidden", "store access denied", 403)
            day = store["snapshot"]["day"]
            records = conn.execute(
                "SELECT day,sku,date,sales_units,price_cents,ended_without_stock "
                "FROM app.observed_sales WHERE tenant_id=%s AND day>=%s AND day<%s "
                "ORDER BY day,sku",
                (tenant, max(1, day - lookback_days), day),
            ).fetchall()
            return {"tenant_id": tenant, "through_day": day - 1, "records": records}

    def get_investigation(self, token, tenant, run_id):
        with transaction(self.dsn, token) as conn:
            require_member(conn, tenant)
            row = conn.execute(
                "SELECT * FROM app.investigations WHERE tenant_id=%s AND run_id=%s",
                (tenant, run_id),
            ).fetchone()
            if not row:
                raise ServiceError("forbidden", "investigation access denied", 403)
            records = conn.execute(
                (
                    "SELECT kind,sequence,data FROM app.thread_records WHERE"
                    " tenant_id=%s AND run_id=%s ORDER BY kind,sequence"
                ),
                (tenant, run_id),
            ).fetchall()
            proposals = conn.execute(
                (
                    "SELECT proposal_id,status,payload_hash FROM app.proposa"
                    "ls WHERE tenant_id=%s AND run_id=%s"
                ),
                (tenant, run_id),
            ).fetchall()
            return dict(row) | {"records": records, "proposals": proposals}

    def propose(
        self,
        tenant,
        run_id,
        actions,
        evidence_ids,
        explanation,
        key,
        *,
        token=None,
        expected_snapshot_version=None,
        expected_policy_version=None,
    ):
        actions = normalize_actions(actions)
        request = {
            "run_id": run_id,
            "actions": [a.model_dump() for a in actions],
            "evidence_ids": list(evidence_ids),
            "explanation": explanation,
            "snapshot_version": expected_snapshot_version,
            "policy_version": expected_policy_version,
        }
        with transaction(self.dsn, token) as conn:
            actor = (
                require_member(conn, tenant, True)["user_id"]
                if token
                else conn.execute("SELECT session_user AS name").fetchone()["name"]
            )

            def create():
                store = locked_store(conn, tenant)
                run = conn.execute(
                    "SELECT 1 FROM app.investigations WHERE tenant_id=%s AND run_id=%s",
                    (tenant, run_id),
                ).fetchone()
                if not run:
                    raise ServiceError("forbidden", "investigation access denied", 403)
                snapshot = StoreSnapshot.model_validate(store["snapshot"])
                now = datetime.now(UTC)
                payload = ProposalPayload(
                    tenant_id=tenant,
                    run_id=run_id,
                    actions=actions,
                    evidence_ids=tuple(evidence_ids),
                    snapshot_version=expected_snapshot_version,
                    policy_version=expected_policy_version,
                    simulated_day=snapshot.day,
                    created_at=now,
                    expires_at=now + timedelta(minutes=15),
                )
                check_payload(payload, snapshot, policy_config(conn, store))
                proposal_id, sha = uuid4(), payload_hash(payload)
                conn.execute(
                    (
                        "INSERT INTO app.proposals(tenant_id,proposal_id,run_id,"
                        "payload,payload_hash) VALUES(%s,%s,%s,%s,%s)"
                    ),
                    (tenant, proposal_id, run_id, Jsonb(payload.model_dump(mode="json")), sha),
                )
                if token is None:
                    conn.execute(
                        (
                            "UPDATE app.investigations SET state='PROPOSED' WHERE te"
                            "nant_id=%s AND run_id=%s AND state<>'LEASED'"
                        ),
                        (tenant, run_id),
                    )
                audit(
                    conn,
                    tenant,
                    "proposal_submitted",
                    actor,
                    {
                        "proposal_id": str(proposal_id),
                        "payload_hash": sha,
                        "explanation": explanation,
                    },
                )
                return {"proposal_id": str(proposal_id), "payload_hash": sha, "status": "PENDING"}

            return idempotent(conn, tenant, actor, "propose", key, request, create)

    def get_proposal(self, token, tenant, proposal_id):
        with transaction(self.dsn, token) as conn:
            require_member(conn, tenant)
            row = conn.execute(
                "SELECT * FROM app.proposals WHERE tenant_id=%s AND proposal_id=%s",
                (tenant, UUID(str(proposal_id))),
            ).fetchone()
            if not row:
                raise ServiceError("forbidden", "proposal access denied", 403)
            receipt = conn.execute(
                "SELECT receipt FROM app.executions WHERE tenant_id=%s AND proposal_id=%s",
                (tenant, row["proposal_id"]),
            ).fetchone()
            return dict(row) | {"execution": receipt["receipt"] if receipt else None}

    def decide(self, token, tenant, proposal_id, expected_hash, key, approve=True):
        proposal_id = UUID(str(proposal_id))
        with transaction(self.dsn, token) as conn:
            actor = require_member(conn, tenant, True)

            def decide():
                store = locked_store(conn, tenant)  # Global order: store, proposal, job.
                proposal = conn.execute(
                    "SELECT * FROM app.proposals WHERE tenant_id=%s AND proposal_id=%s FOR UPDATE",
                    (tenant, proposal_id),
                ).fetchone()
                if not proposal:
                    raise ServiceError("forbidden", "proposal access denied", 403)
                payload = ProposalPayload.model_validate(proposal["payload"])
                sha = payload_hash(payload)
                if sha != proposal["payload_hash"] or not hmac.compare_digest(sha, expected_hash):
                    raise ServiceError("hash_mismatch", "approval does not match immutable payload")
                if approve and proposal["status"] in {"APPROVED", "EXECUTED"}:
                    job = conn.execute(
                        (
                            "SELECT job_id FROM app.execution_jobs WHERE tenant_id=%"
                            "s AND proposal_id=%s"
                        ),
                        (tenant, proposal_id),
                    ).fetchone()
                    return {
                        "proposal_id": str(proposal_id),
                        "status": proposal["status"],
                        "job_id": str(job["job_id"]),
                    }
                if proposal["status"] != "PENDING":
                    raise ServiceError(
                        "decision_conflict", "proposal already has a terminal decision"
                    )
                if approve:
                    check_payload(
                        payload,
                        StoreSnapshot.model_validate(store["snapshot"]),
                        policy_config(conn, store),
                    )
                    conn.execute(
                        (
                            "INSERT INTO app.approvals(tenant_id,proposal_id,payload"
                            "_hash,approver_user_id,session_id) VALUES(%s,%s,%s,%s,%"
                            "s)"
                        ),
                        (tenant, proposal_id, sha, actor["user_id"], actor["session_id"]),
                    )
                status = "APPROVED" if approve else "REJECTED"
                conn.execute(
                    "UPDATE app.proposals SET status=%s WHERE tenant_id=%s AND proposal_id=%s",
                    (status, tenant, proposal_id),
                )
                result = {"proposal_id": str(proposal_id), "status": status}
                if approve:
                    job_id = uuid5(NAMESPACE_URL, f"execute:{tenant}:{proposal_id}")
                    conn.execute(
                        (
                            "INSERT INTO app.execution_jobs(tenant_id,proposal_id,jo"
                            "b_id) VALUES(%s,%s,%s)"
                        ),
                        (tenant, proposal_id, job_id),
                    )
                    result["job_id"] = str(job_id)
                audit(
                    conn,
                    tenant,
                    "proposal_" + status.lower(),
                    actor["user_id"],
                    {"proposal_id": str(proposal_id), "payload_hash": sha},
                )
                return result

            return idempotent(
                conn,
                tenant,
                actor["user_id"],
                "approve" if approve else "reject",
                key,
                {"proposal_id": str(proposal_id), "payload_hash": expected_hash},
                decide,
            )

    def set_execution(self, token, tenant, enabled):
        with transaction(self.dsn, token) as conn:
            require_member(conn, tenant)
            if type(enabled) is not bool:
                raise ServiceError("invalid_flag", "execution flag must be boolean", 422)
            row = conn.execute(
                "SELECT authn.set_execution(%s,%s) AS enabled", (tenant, enabled)
            ).fetchone()
            return {"execution_enabled": row["enabled"]}

    def request_advance(self, token, tenant, snapshot_version, key):
        if type(snapshot_version) is not int or snapshot_version < 1:
            raise ServiceError("invalid_version", "positive integer snapshot version required", 422)
        with transaction(self.dsn, token) as conn:
            actor = require_member(conn, tenant)
            if actor["role"] != "admin":
                raise ServiceError("forbidden", "sandbox admin required", 403)

            def create():
                store = locked_store(conn, tenant)
                if not store["execution_enabled"]:
                    raise ServiceError("disabled", "sandbox mutations are paused")
                if store["snapshot"]["snapshot_version"] != snapshot_version:
                    raise ServiceError("stale", "stale snapshot")
                review_gate(conn, tenant)
                job_id = uuid4()
                conn.execute(
                    (
                        "INSERT INTO app.sandbox_jobs(tenant_id,job_id,snapshot_"
                        "version,actor_id,session_id) VALUES(%s,%s,%s,%s,%s)"
                    ),
                    (tenant, job_id, snapshot_version, actor["user_id"], actor["session_id"]),
                )
                return {"job_id": str(job_id), "state": "QUEUED"}

            return idempotent(
                conn,
                tenant,
                actor["user_id"],
                "advance",
                key,
                {"snapshot_version": snapshot_version},
                create,
            )

    def get_job(self, token, tenant, job_id, kind="execution"):
        from psycopg import sql

        table = "execution_jobs" if kind == "execution" else "sandbox_jobs"
        with transaction(self.dsn, token) as conn:
            require_member(conn, tenant)
            result = conn.execute(
                sql.SQL("SELECT * FROM app.{} WHERE tenant_id=%s AND job_id=%s").format(
                    sql.Identifier(table)
                ),
                (tenant, UUID(str(job_id))),
            ).fetchone()
            if not result:
                raise ServiceError("forbidden", "job access denied", 403)
            return dict(result)

    def get_execution(self, token, tenant, execution_id):
        with transaction(self.dsn, token) as conn:
            require_member(conn, tenant)
            row = conn.execute(
                "SELECT receipt FROM app.executions WHERE tenant_id=%s AND execution_id=%s",
                (tenant, UUID(str(execution_id))),
            ).fetchone()
            if not row:
                raise ServiceError("forbidden", "receipt access denied", 403)
            return row["receipt"]

    def list_alerts(self, token, tenant, status=None):
        if status not in {None, "OPEN", "RESOLVED"}:
            raise ServiceError("status", "status must be OPEN or RESOLVED", 422)
        with transaction(self.dsn, token) as conn:
            require_member(conn, tenant)
            alerts = conn.execute(
                "SELECT alert_id,alert_type,sku,order_id,severity,status,evidence,"
                "detector_version,opened_day,last_day,resolved_day,last_snapshot_version "
                "FROM app.alerts WHERE tenant_id=%s AND (%s::text IS NULL OR status=%s) "
                "ORDER BY status,opened_day,alert_type,sku",
                (tenant, status, status),
            ).fetchall()
            events = conn.execute(
                "SELECT alert_id,kind,day,snapshot_version,severity FROM app.alert_events "
                "WHERE tenant_id=%s ORDER BY created_at",
                (tenant,),
            ).fetchall()
            history = {}
            for event in events:
                history.setdefault(event["alert_id"], []).append(
                    {k: v for k, v in event.items() if k != "alert_id"}
                )
            return {
                "tenant_id": tenant,
                "alerts": [a | {"events": history.get(a["alert_id"], [])} for a in alerts],
            }
