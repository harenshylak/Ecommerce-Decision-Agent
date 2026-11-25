"""PostgreSQL migrations, scoped connections, and administrator-only provisioning."""

import hashlib
import json
import secrets
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from ecommerce_agent.local_database import private_json

GROUPS = (
    "ec_owner",
    "ec_api",
    "ec_agent",
    "ec_executor",
    "ec_simulator",
    "ec_monitor",
    "ec_scheduler",
)


class ServiceError(Exception):
    def __init__(self, code: str, message: str, status=409):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


def digest(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@contextmanager
def transaction(dsn, token=None):
    with psycopg.connect(
        dsn,
        row_factory=dict_row,
        connect_timeout=3,
        options="-c statement_timeout=15000 -c lock_timeout=10000",
    ) as conn:
        if token is not None:
            if not isinstance(token, str) or not 32 <= len(token) <= 256:
                raise ServiceError("unauthenticated", "valid bearer session required", 401)
            conn.execute("SELECT set_config('app.token_hash',%s,true)", (token_hash(token),))
        yield conn


def identity(conn):
    runtime = conn.execute(
        "SELECT rolname,rolsuper,rolbypassrls,pg_has_role(sessio"
        "n_user,'ec_api','MEMBER') AS api FROM pg_roles WHERE ro"
        "lname=session_user"
    ).fetchone()
    if not runtime["api"] or runtime["rolsuper"] or runtime["rolbypassrls"]:
        raise ServiceError("runtime_role", "restricted API credential required", 503)
    result = conn.execute("SELECT * FROM authn.session_identity()").fetchone()
    if result is None:
        raise ServiceError("unauthenticated", "invalid, expired, or revoked session", 401)
    return result


def require_member(conn, tenant, approver=False):
    actor = identity(conn)
    role = conn.execute("SELECT authn.member_role(%s) AS role", (tenant,)).fetchone()["role"]
    if role is None or (approver and role not in {"approver", "admin"}):
        raise ServiceError("forbidden", "store access denied", 403)
    return actor | {"role": role}


def create_database(admin_dsn, database):
    if not database.replace("_", "").isalnum() or len(database) > 50:
        raise ValueError("invalid local database name")
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        for role in GROUPS:
            if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)).fetchone():
                conn.execute(
                    sql.SQL(
                        "CREATE ROLE {} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS"
                    ).format(sql.Identifier(role))
                )
        if not conn.execute("SELECT 1 FROM pg_database WHERE datname=%s", (database,)).fetchone():
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
        conn.execute(
            sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(sql.Identifier(database))
        )
    params = conninfo_to_dict(admin_dsn)
    params["dbname"] = database
    return make_conninfo(**params)


def migrate(admin_dsn):
    with psycopg.connect(admin_dsn) as conn:
        conn.execute("SELECT pg_advisory_xact_lock(7941372)")
        for schema in ("authn", "app", "evaluator", "meta"):
            conn.execute(
                sql.SQL("CREATE SCHEMA IF NOT EXISTS {} AUTHORIZATION ec_owner").format(
                    sql.Identifier(schema)
                )
            )
        conn.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
        conn.execute("SET LOCAL ROLE ec_owner")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS meta.migrations(version text"
            " PRIMARY KEY,sha256 text NOT NULL)"
        )
        for migration in sorted(Path(__file__).with_name("sql").glob("*.sql")):
            content = migration.read_text()
            sha = hashlib.sha256(content.encode()).hexdigest()
            version = migration.name.split("_", 1)[0]
            applied = conn.execute(
                "SELECT sha256 FROM meta.migrations WHERE version=%s", (version,)
            ).fetchone()
            if applied:
                if applied[0] != sha:
                    raise ValueError("applied migration checksum changed; add a new migration")
                continue
            conn.execute(content)
            conn.execute("INSERT INTO meta.migrations VALUES(%s,%s)", (version, sha))


def provision_runtime_roles(admin_dsn, tenants, credential_path: Path):
    if credential_path.exists():
        return json.loads(credential_path.read_text())
    params = conninfo_to_dict(admin_dsn)
    suffix = hashlib.sha256(params["dbname"].encode()).hexdigest()[:10]
    result = {}
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        roles = [("api", "ec_api", None)]
        roles += [
            (f"{kind}_{index}", f"ec_{kind}", tenant)
            for index, tenant in enumerate(tenants)
            for kind in ("agent", "executor", "simulator", "monitor", "scheduler")
        ]
        for label, group, tenant in roles:
            name = f"ec_{suffix}_{label}"
            password = secrets.token_urlsafe(32)
            if conn.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (name,)).fetchone():
                raise ValueError(
                    "runtime role exists without its credential file; do not rotate implicitly"
                )
            conn.execute(
                sql.SQL(
                    "CREATE ROLE {} LOGIN INHERIT NOSUPERUSER NOCREATEDB NOC"
                    "REATEROLE NOBYPASSRLS PASSWORD {}"
                ).format(sql.Identifier(name), sql.Literal(password))
            )
            conn.execute(
                sql.SQL("GRANT {} TO {}").format(sql.Identifier(group), sql.Identifier(name))
            )
            conn.execute(
                sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                    sql.Identifier(params["dbname"]), sql.Identifier(name)
                )
            )
            if tenant is not None:
                conn.execute(
                    "INSERT INTO authn.service_bindings VALUES(%s,%s,%s)",
                    (name, tenant, group.removeprefix("ec_")),
                )
            role_params = params | {"user": name, "password": password}
            result[label] = make_conninfo(**role_params)
    private_json(credential_path, result)
    return result


def issue_session(admin_dsn, user_id, hours=24):
    token = secrets.token_urlsafe(32)
    with transaction(admin_dsn) as conn:
        conn.execute(
            (
                "INSERT INTO authn.sessions(session_id,token_hash,user_i"
                "d,expires_at) VALUES(%s,%s,%s,%s)"
            ),
            (uuid4(), token_hash(token), user_id, datetime.now(UTC) + timedelta(hours=hours)),
        )
    return token


def seed_store(admin_dsn, initial, config, checkpoint):
    snapshot = initial.snapshot
    with transaction(admin_dsn) as conn:
        conn.execute(
            "INSERT INTO app.policy_versions VALUES(%s,%s,%s) ON CONFLICT DO NOTHING",
            (snapshot.tenant_id, snapshot.policy_version, Jsonb(config.model_dump(mode="json"))),
        )
        inserted = conn.execute(
            (
                "INSERT INTO app.stores(tenant_id,snapshot,policy_versio"
                "n,calendar_origin) VALUES(%s,%s,%s,%s) ON CONFLICT DO N"
                "OTHING RETURNING tenant_id"
            ),
            (
                snapshot.tenant_id,
                Jsonb(snapshot.model_dump(mode="json")),
                snapshot.policy_version,
                initial.world.first_date,
            ),
        ).fetchone()
        if not inserted:
            return False
        conn.execute(
            "INSERT INTO evaluator.sandbox_state(tenant_id,checkpoint) VALUES(%s,%s)",
            (snapshot.tenant_id, Jsonb(checkpoint)),
        )
        for order in snapshot.open_purchase_orders:
            conn.execute(
                "INSERT INTO app.purchase_orders(tenant_id,order_id,sku,data) VALUES(%s,%s,%s,%s)",
                (
                    snapshot.tenant_id,
                    order.order_id,
                    order.sku,
                    Jsonb(order.model_dump(mode="json")),
                ),
            )
    return True


def configure_store(admin_dsn, tenant, *, mode, controller):
    """Administrator-only: choose a store's mode and controller before any investigation."""
    with transaction(admin_dsn) as conn:
        updated = conn.execute(
            "UPDATE app.stores SET mode=%s,controller=%s WHERE tenant_id=%s RETURNING tenant_id",
            (mode, controller, tenant),
        ).fetchone()
        if not updated:
            raise ValueError("unknown store")
