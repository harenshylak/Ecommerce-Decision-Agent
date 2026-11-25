"""Phase 3A exit gate: shared monitor detectors and durable alerts on real PostgreSQL."""

from datetime import date, timedelta

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from ecommerce_agent.api import create_app
from ecommerce_agent.contracts import PurchaseOrder, StoreSnapshot
from ecommerce_agent.fixtures import build_fixture
from ecommerce_agent.forecasting import Forecast, ForecastPoint, SalesHistory, SalesRecord
from ecommerce_agent.monitoring import Monitor, covered_days, detect
from ecommerce_agent.oversight import Oversight
from ecommerce_agent.persistence import (
    ServiceError,
    transaction,
)

ORIGIN = date(2026, 1, 1)
FAST, STANDARD = "SYNTH-001", "SYNTH-002"  # Three-day and seven-day suppliers.


def snapshot(stock=20, day=1, version=1, orders=()):
    data = build_fixture().model_dump()
    data.update(tenant_id="store-a", day=day, snapshot_version=version)
    for p in data["products"]:
        p.update(price_cents=200, reference_price_cents=200, cost_cents=100)
    for row in data["inventory"]:
        row["stock"] = stock(row["sku"]) if callable(stock) else stock
    data["open_purchase_orders"] = [o.model_dump() for o in orders]
    return StoreSnapshot.model_validate(data)


def flat_forecasts(snap, rate=1.0):
    return {
        p.sku: Forecast(
            sku=p.sku,
            cutoff=ORIGIN,
            model_id="weekday_mean_4",
            points=tuple(
                ForecastPoint(date=ORIGIN + timedelta(days=i), mean_units=rate) for i in range(14)
            ),
            source_id="test",
        )
        for p in snap.products
    }


def point(mean, upper=None):
    return ForecastPoint(date=ORIGIN, mean_units=mean, upper_80_units=upper)


def kinds(signals):
    return sorted((s.alert_type, s.sku, s.severity) for s in signals)


# Pure detector behavior -------------------------------------------------------------


def test_covered_days_counts_arrivals_and_fractional_demand():
    assert covered_days(5, [1.0] * 14, {}) == 5
    assert covered_days(5, [1.0] * 14, {3: 10}) == 14
    assert covered_days(0, [0.0] * 14, {}) == 14


def test_quiet_store_raises_nothing():
    snap = snapshot(stock=20)
    assert detect(snap, flat_forecasts(snap), {}) == []


def test_stockout_severity_follows_lead_time_plus_buffer():
    snap = snapshot(stock=lambda sku: 5 if sku in (FAST, STANDARD) else 20)
    signals = detect(snap, flat_forecasts(snap), {})
    # Fast supplier: 5 days cover < 3+3 -> medium. Standard: 5 < 7 lead -> high.
    assert kinds(signals) == [
        ("stockout_risk", FAST, "medium"),
        ("stockout_risk", STANDARD, "high"),
    ]
    evidence = next(s for s in signals if s.sku == STANDARD).evidence
    assert evidence["covered_days"] == 5 and evidence["earliest_new_order_arrival_day"] == 8


def test_zero_demand_never_invents_a_stockout_but_flags_overstock():
    snap = snapshot(stock=lambda sku: 0 if sku == FAST else 20 if sku != STANDARD else 8)
    forecasts = flat_forecasts(snap)
    for sku in (FAST, STANDARD):
        forecasts[sku] = forecasts[sku].model_copy(
            update={
                "points": tuple(
                    p.model_copy(update={"mean_units": 0.0}) for p in forecasts[sku].points
                )
            }
        )
    signals = detect(snap, forecasts, {})
    assert kinds(signals) == [("overstock", STANDARD, "low")]
    assert signals[0].evidence["no_forecast_demand"] is True


def test_inbound_order_covers_the_gap_and_overstock_counts_pipeline():
    order = PurchaseOrder(
        order_id="po-1",
        sku=FAST,
        supplier_id="supplier-fast",
        quantity=60,
        created_day=1,
        visible_eta_day=4,
        status="pending",
    )
    snap = snapshot(stock=lambda sku: 5 if sku == FAST else 20, orders=(order,))
    signals = detect(snap, flat_forecasts(snap), {})
    # 5 on hand + 60 inbound = 65 days of cover: no stockout, medium overstock (> 60 days).
    assert kinds(signals) == [("overstock", FAST, "medium")]


def test_moved_eta_and_overdue_orders_raise_supplier_delay():
    moved = PurchaseOrder(
        order_id="po-moved",
        sku=FAST,
        supplier_id="supplier-fast",
        quantity=5,
        created_day=1,
        visible_eta_day=7,
        status="pending",
    )
    snap = snapshot(stock=lambda sku: 2 if sku == FAST else 20, day=4, orders=(moved,))
    delay = [s for s in detect(snap, flat_forecasts(snap), {}) if s.alert_type == "supplier_delay"]
    assert len(delay) == 1 and delay[0].severity == "high"  # Runs out before day 7.
    assert (delay[0].evidence["reason"], delay[0].evidence["delay_days"]) == ("eta_moved", 3)
    overdue = moved.model_copy(update={"visible_eta_day": 4})
    snap = snapshot(stock=20, day=6, orders=(overdue,))
    delay = [s for s in detect(snap, flat_forecasts(snap), {}) if s.alert_type == "supplier_delay"]
    assert [(s.evidence["reason"], s.severity) for s in delay] == [("overdue", "high")]


def test_demand_spike_requires_ratio_and_minimum_extra_units():
    snap = snapshot(stock=40, day=4)

    def rows(units, out=False):
        return [{"day": d, "sales_units": units, "ended_without_stock": out} for d in (1, 2, 3)]

    spike = {p.sku: [point(2.0, 3.5)] * 3 for p in snap.products}
    recent = {p.sku: rows(2) for p in snap.products}
    recent[FAST] = rows(4, out=True)  # 12 vs 6: ratio 2, +6 units, sold out.
    recent[STANDARD] = rows(3)  # 9 vs 6: ratio 1.5 but only +3 units -> quiet.
    signals = [
        s
        for s in detect(snap, flat_forecasts(snap), recent, spike)
        if s.alert_type == "demand_spike"
    ]
    assert kinds(signals) == [("demand_spike", FAST, "high")]
    assert signals[0].evidence["stockout_days_in_window"] == [1, 2, 3]
    assert "true demand may be higher" in signals[0].evidence["note"]


def test_spike_inside_the_forecast_band_is_not_an_alert():
    snap = snapshot(stock=20, day=4)
    recent = {FAST: [{"day": d, "sales_units": 4, "ended_without_stock": False} for d in (1, 2, 3)]}
    # 12 units: 2x the mean and +6, but within the summed 80% band (3 x 4.5 = 13.5).
    wide = {FAST: [point(2.0, 4.5)] * 3}
    assert [s for s in detect(snap, flat_forecasts(snap), recent, wide) if s.sku == FAST] == []
    narrow = {FAST: [point(2.0, 3.0)] * 3}
    signals = detect(snap, flat_forecasts(snap), recent, narrow)
    assert [(s.alert_type, s.evidence["expected_upper_80_units"]) for s in signals] == [
        ("demand_spike", 9.0)
    ]


def test_overstock_hysteresis_keeps_an_open_alert_until_below_resolve_line():
    snap = snapshot(stock=lambda sku: 28 if sku == FAST else 20)  # 28 days of cover.
    assert detect(snap, flat_forecasts(snap), {}) == []  # Not above 30: no new alert.
    held = detect(snap, flat_forecasts(snap), {}, open_keys=frozenset({f"overstock:{FAST}"}))
    assert kinds(held) == [("overstock", FAST, "low")]  # Already open, still above 25.
    snap = snapshot(stock=lambda sku: 24 if sku == FAST else 20)
    assert detect(snap, flat_forecasts(snap), {}, open_keys=frozenset({f"overstock:{FAST}"})) == []


# Durable monitor on PostgreSQL ------------------------------------------------------


def base_history(skus, rate=1):
    records = [
        SalesRecord(sku=sku, date=ORIGIN - timedelta(days=offset), sales_units=rate)
        for sku in skus
        for offset in range(1, 57)
    ]
    return SalesHistory("test", tuple(records), "test-history")


def publish(env, snap, tenant="store-a"):
    """Administrator stands in for the simulator/executor publishing a new public snapshot."""
    snap = snap.model_copy(update={"tenant_id": tenant})
    with transaction(env["admin"]) as conn:
        current = conn.execute(
            "SELECT snapshot FROM app.stores WHERE tenant_id=%s", (tenant,)
        ).fetchone()["snapshot"]
        snap = snap.model_copy(update={"policy_version": current["policy_version"]})
        for day in range(1, snap.day):  # Completed public sales; tests may pre-insert their own.
            for p in snap.products:
                conn.execute(
                    "INSERT INTO app.observed_sales VALUES(%s,%s,%s,%s,1,200,false) "
                    "ON CONFLICT DO NOTHING",
                    (tenant, day, p.sku, ORIGIN + timedelta(days=day - 1)),
                )
        conn.execute(
            "UPDATE app.stores SET snapshot=%s WHERE tenant_id=%s",
            (Jsonb(snap.model_dump(mode="json")), tenant),
        )
    return snap


def monitor(env, tenant="store-a", role="monitor_0", **kwargs):
    return Monitor(env["services"][role], tenant, env["history"], **kwargs)


def alerts(env, tenant="store-a"):
    with transaction(env["admin"]) as conn:
        return conn.execute(
            "SELECT * FROM app.alerts WHERE tenant_id=%s ORDER BY opened_snapshot_version,sku",
            (tenant,),
        ).fetchall()


def test_initial_quiet_state_is_observed_once_and_stays_quiet(env):
    first = monitor(env).run_pending()
    assert [(r["snapshot_version"], r["signals"]) for r in first] == [(1, 0)]
    assert monitor(env).run_pending() == []
    assert alerts(env) == []


def test_shortage_creates_one_alert_updates_on_repeat_and_resolves(env):
    low = lambda sku: 5 if sku == STANDARD else 20  # noqa: E731
    publish(env, snapshot(stock=low, version=2))
    publish(env, snapshot(stock=low, version=3))  # Same issue, newer version.
    monitor(env).run_pending()
    rows = alerts(env)
    assert [(r["alert_type"], r["sku"], r["severity"], r["status"]) for r in rows] == [
        ("stockout_risk", STANDARD, "high", "OPEN")
    ]
    assert rows[0]["opened_snapshot_version"] == 2 and rows[0]["last_snapshot_version"] == 3
    evidence = rows[0]["evidence"]
    assert evidence["snapshot_version"] == 3 and evidence["forecast_source_id"].startswith(
        "history"
    )
    publish(env, snapshot(stock=20, version=4))
    monitor(env).run_pending()
    rows = alerts(env)
    assert len(rows) == 1 and rows[0]["status"] == "RESOLVED" and rows[0]["resolved_day"] == 1
    with transaction(env["admin"]) as conn:
        events = [
            r["kind"]
            for r in conn.execute(
                "SELECT kind FROM app.alert_events ORDER BY created_at"
            ).fetchall()
        ]
    assert events == ["opened", "resolved"]


def test_released_supplier_delay_alert_traces_public_eta(env):
    order = PurchaseOrder(
        order_id="po-1",
        sku=FAST,
        supplier_id="supplier-fast",
        quantity=5,
        created_day=1,
        visible_eta_day=4,
        status="pending",
    )
    publish(env, snapshot(stock=20, version=2, orders=(order,)))
    delayed = order.model_copy(update={"visible_eta_day": 7})
    publish(env, snapshot(stock=20, day=4, version=3, orders=(delayed,)))
    publish(env, snapshot(stock=20, day=7, version=4))  # Received; no longer open.
    monitor(env).run_pending()
    rows = [r for r in alerts(env) if r["alert_type"] == "supplier_delay"]
    assert len(rows) == 1
    assert rows[0]["order_id"] == "po-1" and rows[0]["opened_day"] == 4
    assert rows[0]["evidence"]["published_eta_day"] == 7 and rows[0]["status"] == "RESOLVED"


def test_crash_before_commit_then_restart_neither_skips_nor_duplicates(env):
    publish(env, snapshot(stock=lambda sku: 5 if sku == STANDARD else 20, version=2))

    def crash(stage):
        raise RuntimeError(stage)

    with pytest.raises(RuntimeError):
        monitor(env, failpoint=crash).run_pending()
    assert alerts(env) == []
    processed = monitor(env).run_pending()
    assert [r["snapshot_version"] for r in processed] == [1, 2]
    assert len(alerts(env)) == 1


def test_spike_uses_only_completed_public_sales(env):
    with transaction(env["admin"]) as conn:
        for day in (1, 2, 3):
            for p in snapshot().products:
                units = 4 if p.sku == FAST else 1
                conn.execute(
                    "INSERT INTO app.observed_sales VALUES(%s,%s,%s,%s,%s,%s,%s)",
                    ("store-a", day, p.sku, ORIGIN + timedelta(days=day - 1), units, 200, False),
                )
    publish(env, snapshot(stock=40, day=4, version=5))
    monitor(env).run_pending()
    spikes = [r for r in alerts(env) if r["alert_type"] == "demand_spike"]
    assert [(r["sku"], r["severity"]) for r in spikes] == [(FAST, "high")]
    assert spikes[0]["evidence"]["actual_units"] == 12


def test_monitor_role_is_tenant_bound_and_cannot_read_private_truth(env):
    with pytest.raises(ServiceError):
        monitor(env, tenant="store-b", role="monitor_0").process_next()
    for query in (
        "SELECT * FROM evaluator.sandbox_state",
        "UPDATE app.stores SET snapshot=snapshot",
        "INSERT INTO app.proposals SELECT * FROM app.proposals",
        "INSERT INTO app.approvals SELECT * FROM app.approvals",
    ):
        with (
            pytest.raises(psycopg.errors.InsufficientPrivilege),
            transaction(env["services"]["monitor_0"]) as conn,
        ):
            conn.execute(query)
    with transaction(env["services"]["monitor_0"]) as conn:
        tenants = {
            r["tenant_id"]
            for r in conn.execute("SELECT tenant_id FROM app.store_observations").fetchall()
        }
    assert tenants == {"store-a"}


def test_alerts_api_is_member_scoped(env):
    publish(env, snapshot(stock=lambda sku: 5 if sku == STANDARD else 20, version=2))
    monitor(env).run_pending()
    service = Oversight(env["services"]["api"])
    listed = service.list_alerts(env["tokens"]["viewer-a"], "store-a", "OPEN")["alerts"]
    assert [(a["alert_type"], [e["kind"] for e in a["events"]]) for a in listed] == [
        ("stockout_risk", ["opened"])
    ]
    with pytest.raises(ServiceError):
        service.list_alerts(env["tokens"]["merchant-b"], "store-a")
    with TestClient(create_app(env["services"]["api"])) as client:
        response = client.get(
            "/alerts",
            headers={
                "Authorization": "Bearer " + env["tokens"]["merchant-a"],
                "X-Tenant-ID": "store-a",
            },
        )
        assert response.status_code == 200 and len(response.json()["alerts"]) == 1
