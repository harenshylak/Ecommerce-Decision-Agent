"""Separately credentialed, leased executor and simulator workers."""

import hmac
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from psycopg import sql
from psycopg.types.json import Jsonb

from ecommerce_agent.contracts import ProposalPayload, StoreSnapshot, payload_hash
from ecommerce_agent.durable_simulator import checkpoint, reconcile_receipts, restore
from ecommerce_agent.execution import apply_actions
from ecommerce_agent.oversight import (
    audit,
    check_payload,
    locked_store,
    policy_config,
    review_gate,
)
from ecommerce_agent.persistence import ServiceError, transaction


class Worker:
    def __init__(self, dsn, tenant, kind="executor", *, lease_seconds=30, failpoint=None):
        if kind not in {"executor", "simulator"}:
            raise ValueError("unknown worker kind")
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 300:
            raise ValueError("lease must be 1-300 seconds")
        self.dsn, self.tenant, self.kind = dsn, tenant, kind
        self.table = "execution_jobs" if kind == "executor" else "sandbox_jobs"
        self.lease_seconds, self.failpoint = lease_seconds, failpoint

    def claim(self):
        owner = uuid4()
        with transaction(self.dsn) as conn:
            conn.execute(
                sql.SQL("""UPDATE app.{} SET state='FAILED',last_error='retry_limit',
                lease_owner=NULL,lease_expires_at=NULL WHERE tenant_id=%s AND attempts>=max_attempts
                AND (state='QUEUED' OR (state='LEASED' AND lease_expires_at<clock_timestamp()))
                """).format(sql.Identifier(self.table)),
                (self.tenant,),
            )
            # Lock only the queue row. Release it before acquiring any store lock.
            row = conn.execute(
                sql.SQL("""
                SELECT j.* FROM app.{} j JOIN app.stores s USING(tenant_id)
                WHERE j.tenant_id=%s AND s.execution_enabled AND j.attempts<j.max_attempts
                  AND j.ready_at<=clock_timestamp()
                  AND (j.state='QUEUED' OR (j.state='LEASED'
                       AND j.lease_expires_at<clock_timestamp()))
                ORDER BY j.ready_at,j.job_id FOR UPDATE OF j SKIP LOCKED LIMIT 1
                """).format(sql.Identifier(self.table)),
                (self.tenant,),
            ).fetchone()
            if not row:
                return None
            conn.execute(
                sql.SQL("""UPDATE app.{} SET state='LEASED',lease_owner=%s,
                lease_expires_at=clock_timestamp()+(%s * interval '1 second'),
                heartbeat_at=clock_timestamp(),attempts=attempts+1
                WHERE tenant_id=%s AND job_id=%s""").format(sql.Identifier(self.table)),
                (owner, self.lease_seconds, self.tenant, row["job_id"]),
            )
            return {"job_id": str(row["job_id"]), "lease_owner": str(owner)}

    def heartbeat(self, job_id, lease_owner):
        with transaction(self.dsn) as conn:
            result = conn.execute(
                sql.SQL("""UPDATE app.{} SET heartbeat_at=clock_timestamp(),
                lease_expires_at=clock_timestamp()+(%s * interval '1 second')
                WHERE tenant_id=%s AND job_id=%s AND state='LEASED' AND lease_owner=%s
                AND lease_expires_at>clock_timestamp() RETURNING job_id""").format(
                    sql.Identifier(self.table)
                ),
                (self.lease_seconds, self.tenant, UUID(str(job_id)), UUID(str(lease_owner))),
            ).fetchone()
            if not result:
                raise ServiceError("lease_lost", "worker lease is no longer current")

    def _job(self, conn, job_id, lease_owner):
        row = conn.execute(
            sql.SQL("SELECT * FROM app.{} WHERE tenant_id=%s AND job_id=%s FOR UPDATE").format(
                sql.Identifier(self.table)
            ),
            (self.tenant, UUID(str(job_id))),
        ).fetchone()
        if (
            not row
            or row["state"] != "LEASED"
            or str(row["lease_owner"]) != str(lease_owner)
            or row["lease_expires_at"] <= datetime.now(UTC)
        ):
            raise ServiceError("lease_lost", "worker lease is no longer current")
        return row

    def execute(self, job_id, lease_owner):
        if self.kind != "executor":
            return self.advance(job_id, lease_owner)
        with transaction(self.dsn) as conn:
            store = locked_store(conn, self.tenant)
            lookup = conn.execute(
                "SELECT proposal_id FROM app.execution_jobs WHERE tenant_id=%s AND job_id=%s",
                (self.tenant, UUID(str(job_id))),
            ).fetchone()
            if not lookup:
                raise ServiceError("forbidden", "job access denied", 403)
            pid = lookup["proposal_id"]
            proposal = conn.execute(
                "SELECT * FROM app.proposals WHERE tenant_id=%s AND proposal_id=%s FOR UPDATE",
                (self.tenant, pid),
            ).fetchone()
            existing = conn.execute(
                "SELECT receipt FROM app.executions WHERE tenant_id=%s AND proposal_id=%s",
                (self.tenant, pid),
            ).fetchone()
            if existing:
                return existing["receipt"]
            self._job(conn, job_id, lease_owner)
            if not store["execution_enabled"]:
                raise ServiceError("disabled", "sandbox mutations are paused")
            if proposal["status"] != "APPROVED":
                raise ServiceError("unapproved", "proposal has no executable approval")
            approval = conn.execute(
                "SELECT * FROM app.approvals WHERE tenant_id=%s AND proposal_id=%s",
                (self.tenant, pid),
            ).fetchone()
            if approval is None:
                raise ServiceError("unapproved", "approval missing")
            valid = conn.execute(
                "SELECT authn.approval_valid(%s,%s,%s) AS valid",
                (self.tenant, approval["approver_user_id"], approval["session_id"]),
            ).fetchone()["valid"]
            if not valid:
                raise ServiceError(
                    "approval_revoked", "approver or session is no longer authorized", 403
                )
            payload = ProposalPayload.model_validate(proposal["payload"])
            sha = payload_hash(payload)
            if not hmac.compare_digest(sha, proposal["payload_hash"]) or not hmac.compare_digest(
                sha, approval["payload_hash"]
            ):
                raise ServiceError("hash_mismatch", "approved payload hash mismatch")
            snapshot = StoreSnapshot.model_validate(store["snapshot"])
            config = policy_config(conn, store)
            check_payload(payload, snapshot, config)
            updated, effects = apply_actions(snapshot, payload.actions, config, str(pid))
            execution_id = uuid5(NAMESPACE_URL, f"receipt:{self.tenant}:{pid}")
            receipt = {
                "execution_id": str(execution_id),
                "proposal_id": str(pid),
                "payload_hash": sha,
                "tenant_id": self.tenant,
                "approver_user_id": approval["approver_user_id"],
                "committed_at": datetime.now(UTC).isoformat(),
                "effects": effects,
                "before": snapshot.model_dump(mode="json"),
                "after": updated.model_dump(mode="json"),
            }
            conn.execute(
                "UPDATE app.stores SET snapshot=%s WHERE tenant_id=%s",
                (Jsonb(receipt["after"]), self.tenant),
            )
            for order in effects["new_orders"]:
                conn.execute(
                    (
                        "INSERT INTO app.purchase_orders(tenant_id,order_id,sku,"
                        "proposal_id,data) VALUES(%s,%s,%s,%s,%s)"
                    ),
                    (self.tenant, order["order_id"], order["sku"], pid, Jsonb(order)),
                )
            conn.execute(
                (
                    "INSERT INTO app.executions(tenant_id,proposal_id,payloa"
                    "d_hash,execution_id,receipt) VALUES(%s,%s,%s,%s,%s)"
                ),
                (self.tenant, pid, sha, execution_id, Jsonb(receipt)),
            )
            conn.execute(
                "UPDATE app.proposals SET status='EXECUTED' WHERE tenant_id=%s AND proposal_id=%s",
                (self.tenant, pid),
            )
            conn.execute(
                (
                    "UPDATE app.execution_jobs SET state='COMPLETED',lease_o"
                    "wner=NULL,lease_expires_at=NULL,last_error=NULL WHERE t"
                    "enant_id=%s AND job_id=%s"
                ),
                (self.tenant, UUID(str(job_id))),
            )
            audit(
                conn,
                self.tenant,
                "proposal_executed",
                self.kind,
                {"execution_id": str(execution_id), "proposal_id": str(pid), "payload_hash": sha},
            )
            if datetime.now(UTC) >= payload.expires_at:
                raise ServiceError("expired", "proposal expired before commit")
            if self.failpoint:
                self.failpoint("before_commit")
        if self.failpoint:
            self.failpoint("after_commit")
        return receipt

    def advance(self, job_id, lease_owner):
        with transaction(self.dsn) as conn:
            store = locked_store(conn, self.tenant)
            done = conn.execute(
                "SELECT state,result FROM app.sandbox_jobs WHERE tenant_id=%s AND job_id=%s",
                (self.tenant, UUID(str(job_id))),
            ).fetchone()
            if done and done["state"] == "COMPLETED":
                return done["result"]
            job = self._job(conn, job_id, lease_owner)
            if not store["execution_enabled"]:
                raise ServiceError("disabled", "sandbox mutations are paused")
            review_gate(conn, self.tenant)
            valid = conn.execute(
                "SELECT authn.lock_identity(%s,%s,%s,true) AS valid",
                (self.tenant, job["actor_id"], job["session_id"]),
            ).fetchone()["valid"]
            if not valid:
                raise ServiceError("approval_revoked", "sandbox admin is no longer authorized", 403)
            before = StoreSnapshot.model_validate(store["snapshot"])
            if job["snapshot_version"] != before.snapshot_version:
                raise ServiceError("stale", "store changed after advance request")
            private = conn.execute(
                "SELECT * FROM evaluator.sandbox_state WHERE tenant_id=%s FOR UPDATE",
                (self.tenant,),
            ).fetchone()
            engine = restore(private["checkpoint"])
            if engine.complete:
                raise ServiceError("episode_finished", "episode already finished")
            applied = set(private["applied_execution_ids"])
            records = conn.execute(
                "SELECT execution_id,receipt FROM app.executions WHERE tenant_id=%s", (self.tenant,)
            ).fetchall()
            receipts = [r["receipt"] for r in records if str(r["execution_id"]) not in applied]
            receipts.sort(key=lambda r: r["before"]["snapshot_version"])
            reconcile_receipts(engine, receipts)
            if engine._snapshot() != before:
                raise ServiceError(
                    "checkpoint_conflict", "public state and private checkpoint disagree"
                )
            completed_day = engine._day
            engine.step()
            for row in engine.sales_observations:
                if row["day"] == completed_day:
                    conn.execute(
                        "INSERT INTO app.observed_sales "
                        "(tenant_id,day,sku,date,sales_units,price_cents,ended_without_stock) "
                        "VALUES(%s,%s,%s,%s,%s,%s,%s)",
                        (
                            self.tenant,
                            row["day"],
                            row["sku"],
                            row["date"],
                            row["sales_units"],
                            row["price_cents"],
                            row["ended_without_stock"],
                        ),
                    )
            updated = engine._snapshot() if engine.complete else engine.observe()
            conn.execute(
                "UPDATE app.stores SET snapshot=%s WHERE tenant_id=%s",
                (Jsonb(updated.model_dump(mode="json")), self.tenant),
            )
            for order in engine._orders.values():
                conn.execute(
                    "UPDATE app.purchase_orders SET data=%s WHERE tenant_id=%s AND order_id=%s",
                    (
                        Jsonb(order.visible.model_dump(mode="json")),
                        self.tenant,
                        order.visible.order_id,
                    ),
                )
            applied.update(str(r["execution_id"]) for r in records)
            conn.execute(
                (
                    "UPDATE evaluator.sandbox_state SET checkpoint=%s,applie"
                    "d_execution_ids=%s WHERE tenant_id=%s"
                ),
                (Jsonb(checkpoint(engine)), Jsonb(sorted(applied)), self.tenant),
            )
            result = {
                "job_id": str(job_id),
                "state": "COMPLETED",
                "day": updated.day,
                "snapshot_version": updated.snapshot_version,
                "episode_complete": engine.complete,
            }
            conn.execute(
                (
                    "UPDATE app.sandbox_jobs SET state='COMPLETED',result=%s"
                    ",lease_owner=NULL,lease_expires_at=NULL,last_error=NULL"
                    " WHERE tenant_id=%s AND job_id=%s"
                ),
                (Jsonb(result), self.tenant, UUID(str(job_id))),
            )
            audit(conn, self.tenant, "sandbox_advanced", job["actor_id"], result)
            if self.failpoint:
                self.failpoint("before_commit")
        if self.failpoint:
            self.failpoint("after_commit")
        return result

    def release(self, job_id, lease_owner, error):
        terminal = isinstance(error, ServiceError) and error.code not in {"disabled", "lease_lost"}
        code = error.code if isinstance(error, ServiceError) else "worker_interrupted"
        with transaction(self.dsn) as conn:
            # Store, then proposal, then queue row, consistent with execution/approval.
            locked_store(conn, self.tenant)
            if self.kind == "executor":
                lookup = conn.execute(
                    "SELECT proposal_id FROM app.execution_jobs WHERE tenant_id=%s AND job_id=%s",
                    (self.tenant, UUID(str(job_id))),
                ).fetchone()
                if lookup:
                    proposal = conn.execute(
                        (
                            "SELECT status FROM app.proposals WHERE tenant_id=%s AND"
                            " proposal_id=%s FOR UPDATE"
                        ),
                        (self.tenant, lookup["proposal_id"]),
                    ).fetchone()
                    owned = conn.execute(
                        (
                            "SELECT state,lease_owner FROM app.execution_jobs WHERE "
                            "tenant_id=%s AND job_id=%s FOR UPDATE"
                        ),
                        (self.tenant, UUID(str(job_id))),
                    ).fetchone()
                    if (
                        not owned
                        or owned["state"] != "LEASED"
                        or str(owned["lease_owner"]) != str(lease_owner)
                    ):
                        return
                    if terminal and proposal and proposal["status"] == "APPROVED":
                        status = (
                            "EXPIRED"
                            if code == "expired"
                            else "STALE"
                            if code == "stale"
                            else "FAILED_SAFE"
                        )
                        conn.execute(
                            (
                                "UPDATE app.proposals SET status=%s WHERE tenant_id=%s A"
                                "ND proposal_id=%s"
                            ),
                            (status, self.tenant, lookup["proposal_id"]),
                        )
            conn.execute(
                sql.SQL("""UPDATE app.{} SET state=CASE
                WHEN %s OR attempts>=max_attempts THEN 'FAILED' ELSE 'QUEUED' END,
                last_error=%s,lease_owner=NULL,lease_expires_at=NULL,
                ready_at=clock_timestamp()+(interval '2 seconds')
                WHERE tenant_id=%s AND job_id=%s AND state='LEASED' AND lease_owner=%s""").format(
                    sql.Identifier(self.table)
                ),
                (terminal, code, self.tenant, UUID(str(job_id)), UUID(str(lease_owner))),
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
