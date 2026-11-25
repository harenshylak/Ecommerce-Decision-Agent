"""Authenticated local oversight API. Executor/simulator credentials stay separate."""

import os
from contextlib import asynccontextmanager
from typing import Annotated
from uuid import UUID

import psycopg
from fastapi import Depends, FastAPI, Header, Query
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import Field, StrictBool, StrictInt

from ecommerce_agent.contracts import FrozenContract
from ecommerce_agent.oversight import Oversight
from ecommerce_agent.persistence import ServiceError


class InvestigationRequest(FrozenContract):
    question: str = Field(min_length=1, max_length=2000, strict=True)


class DecisionRequest(FrozenContract):
    payload_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class ExecutionFlag(FrozenContract):
    enabled: StrictBool


class AdvanceRequest(FrozenContract):
    snapshot_version: Annotated[StrictInt, Field(gt=0)]


def create_app(database_url: str | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.database_url = database_url or os.getenv("API_DATABASE_URL")
        yield

    app = FastAPI(title="Ecommerce Decision Agent", version="0.1.0", lifespan=lifespan)
    bearer = HTTPBearer(auto_error=False)

    @app.exception_handler(ServiceError)
    async def service_error(request, exc):
        headers = {"WWW-Authenticate": "Bearer"} if exc.status == 401 else None
        return JSONResponse(
            {"error": exc.code, "detail": exc.message}, status_code=exc.status, headers=headers
        )

    @app.exception_handler(psycopg.Error)
    async def database_error(request, exc):
        status = (
            403
            if isinstance(exc, psycopg.errors.InsufficientPrivilege)
            else (409 if isinstance(exc, psycopg.IntegrityError) else 503)
        )
        return JSONResponse(
            {
                "error": "database_request_denied"
                if status == 403
                else "database_unavailable_or_conflict"
            },
            status_code=status,
        )

    bearer_dependency = Depends(bearer)

    def token(credentials: HTTPAuthorizationCredentials | None = bearer_dependency):
        if credentials is None or credentials.scheme.lower() != "bearer":
            raise ServiceError("unauthenticated", "bearer authentication required", 401)
        return credentials.credentials

    def service():
        if not app.state.database_url:
            raise ServiceError("unconfigured", "local database is not configured", 503)
        return Oversight(app.state.database_url)

    auth_dependency = Depends(token)
    service_dependency = Depends(service)

    tenant_header = Header(alias="X-Tenant-ID", min_length=1, max_length=128)
    idempotency_header = Header(alias="Idempotency-Key", min_length=1, max_length=128)

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/ready")
    def ready() -> JSONResponse:
        url = app.state.database_url
        if not url:
            return JSONResponse({"status": "not_ready"}, status_code=503)
        try:
            with psycopg.connect(
                url, connect_timeout=2, options="-c statement_timeout=2000"
            ) as conn:
                row = conn.execute(
                    "SELECT rolsuper,rolbypassrls,pg_has_role(session_user,'ec_api','MEMBER') "
                    "FROM pg_roles WHERE rolname=session_user"
                ).fetchone()
                if not row or row[0] or row[1] or not row[2]:
                    return JSONResponse({"status": "not_ready"}, status_code=503)
                conn.execute("SELECT 1 FROM app.stores LIMIT 0")
        except psycopg.Error:
            return JSONResponse({"status": "not_ready"}, status_code=503)
        return JSONResponse({"status": "ready"})

    @app.get("/me")
    def me(auth=auth_dependency, api=service_dependency):
        return api.me(auth)

    @app.get("/stores/{tenant_id}")
    def store(tenant_id: str, auth=auth_dependency, api=service_dependency):
        return api.get_store(auth, tenant_id)

    @app.get("/stores/{tenant_id}/sales")
    def sales(
        tenant_id: str,
        lookback_days: Annotated[int, Query(ge=1, le=365)] = 56,
        auth=auth_dependency,
        api=service_dependency,
    ):
        return api.observed_sales(auth, tenant_id, lookback_days)

    @app.get("/alerts")
    def alerts(
        status: Annotated[str | None, Query(pattern="^(OPEN|RESOLVED)$")] = None,
        tenant: str = tenant_header,
        auth=auth_dependency,
        api=service_dependency,
    ):
        return api.list_alerts(auth, tenant, status)

    @app.post("/investigations", status_code=202)
    def investigate(
        body: InvestigationRequest,
        tenant: str = tenant_header,
        key: str = idempotency_header,
        auth=auth_dependency,
        api=service_dependency,
    ):
        return api.investigate(auth, tenant, body.question, key)

    @app.get("/investigations/{run_id}")
    def investigation(
        run_id: str, tenant: str = tenant_header, auth=auth_dependency, api=service_dependency
    ):
        return api.get_investigation(auth, tenant, run_id)

    @app.get("/proposals/{proposal_id}")
    def proposal(
        proposal_id: UUID, tenant: str = tenant_header, auth=auth_dependency, api=service_dependency
    ):
        return api.get_proposal(auth, tenant, proposal_id)

    @app.post("/proposals/{proposal_id}/approve", status_code=202)
    def approve(
        proposal_id: UUID,
        body: DecisionRequest,
        tenant: str = tenant_header,
        key: str = idempotency_header,
        auth=auth_dependency,
        api=service_dependency,
    ):
        return api.decide(auth, tenant, proposal_id, body.payload_hash, key)

    @app.post("/proposals/{proposal_id}/reject")
    def reject(
        proposal_id: UUID,
        body: DecisionRequest,
        tenant: str = tenant_header,
        key: str = idempotency_header,
        auth=auth_dependency,
        api=service_dependency,
    ):
        return api.decide(auth, tenant, proposal_id, body.payload_hash, key, False)

    @app.get("/executions/{execution_id}")
    def execution(
        execution_id: UUID,
        tenant: str = tenant_header,
        auth=auth_dependency,
        api=service_dependency,
    ):
        return api.get_execution(auth, tenant, execution_id)

    @app.get("/execution-jobs/{job_id}")
    def execution_job(
        job_id: UUID, tenant: str = tenant_header, auth=auth_dependency, api=service_dependency
    ):
        return api.get_job(auth, tenant, job_id)

    @app.post("/admin/execution")
    def execution_flag(
        body: ExecutionFlag,
        tenant: str = tenant_header,
        auth=auth_dependency,
        api=service_dependency,
    ):
        return api.set_execution(auth, tenant, body.enabled)

    @app.post("/sandbox/advance", status_code=202)
    def advance(
        body: AdvanceRequest,
        tenant: str = tenant_header,
        key: str = idempotency_header,
        auth=auth_dependency,
        api=service_dependency,
    ):
        return api.request_advance(auth, tenant, body.snapshot_version, key)

    @app.get("/sandbox/jobs/{job_id}")
    def sandbox_job(
        job_id: UUID, tenant: str = tenant_header, auth=auth_dependency, api=service_dependency
    ):
        return api.get_job(auth, tenant, job_id, "sandbox")

    return app


app = create_app()
