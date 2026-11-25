"""Shared real-PostgreSQL fixtures for the proactive (Phase 3) tests."""

from dataclasses import replace
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from test_monitoring import ORIGIN, base_history, snapshot

from ecommerce_agent.config import load_config
from ecommerce_agent.durable_simulator import checkpoint
from ecommerce_agent.local_database import start_database
from ecommerce_agent.local_setup import seed_identities
from ecommerce_agent.persistence import (
    create_database,
    migrate,
    provision_runtime_roles,
    seed_store,
)
from ecommerce_agent.simulation import Initialization, Scenario, Simulator, World


@pytest.fixture(scope="session")
def postgres_server():
    return start_database()


@pytest.fixture
def env(postgres_server, tmp_path):
    database = "ec_test_" + uuid4().hex[:12]
    admin = create_database(postgres_server, database)
    migrate(admin)
    config = load_config()
    for tenant in ("store-a", "store-b"):
        snap = snapshot(stock=20).model_copy(update={"tenant_id": tenant})
        world = World(
            Scenario(scenario_id=f"test-{tenant}"),
            ORIGIN,
            {p.sku: (1.0,) * 7 for p in snap.products},
            {p.sku: -1.0 for p in snap.products},
        )
        initial = Initialization(snap, world)
        engine = Simulator(initial, config)
        seed_store(admin, replace(initial, snapshot=engine.observe()), config, checkpoint(engine))
    services = provision_runtime_roles(admin, ["store-a", "store-b"], tmp_path / "services.json")
    tokens = seed_identities(admin)
    skus = [p.sku for p in snapshot().products]
    try:
        yield {
            "admin": admin,
            "services": services,
            "tokens": tokens,
            "history": base_history(skus),
        }
    finally:
        with psycopg.connect(postgres_server, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database)))
            for dsn in services.values():
                conn.execute(
                    sql.SQL("DROP ROLE {}").format(sql.Identifier(conninfo_to_dict(dsn)["user"]))
                )
