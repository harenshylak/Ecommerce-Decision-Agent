"""Phase 3A shared monitor: deterministic detectors over public observations, durable alerts.

No model calls. Detectors see only a released snapshot, completed (fulfilled) sales and a
forecast computed from them. Hidden demand, actual arrival days and elasticities stay private.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, timedelta
from uuid import uuid4

from psycopg.types.json import Jsonb

from ecommerce_agent.contracts import StoreSnapshot
from ecommerce_agent.forecasting import SalesHistory, SalesRecord, forecast_demand
from ecommerce_agent.persistence import ServiceError, transaction

DETECTOR_VERSION = "monitor-0.2.0"
SEVERITY_RANK = {"low": 0, "medium": 1, "high": 2}


@dataclass(frozen=True)
class Thresholds:
    """Fixed before any performance measurement (user decisions, Nov 24 2026).

    0.2.0: a spike must also exceed the forecast's summed 80% upper band (development runs:
    false alarms 14% -> 5%), and overstock uses hysteresis (open above 30 days, resolve below 25).
    """

    stockout_buffer_days: int = 3  # alert if cover < lead time + buffer; HIGH if < lead time
    spike_window_days: int = 3
    spike_ratio: float = 1.5  # HIGH at 2x
    spike_high_ratio: float = 2.0
    spike_min_extra_units: float = 5.0
    spike_require_upper_band: bool = True
    overstock_cover_days: int = 30  # MEDIUM at 2x
    overstock_resolve_days: int = 25  # an open overstock alert stays open until cover drops below
    overstock_min_units: int = 5
    horizon_days: int = 14


THRESHOLDS = Thresholds()


@dataclass(frozen=True)
class Signal:
    alert_type: str
    sku: str
    severity: str
    evidence: dict
    order_id: str | None = None

    @property
    def dedup_key(self) -> str:
        return f"{self.alert_type}:{self.order_id or self.sku}"


def covered_days(stock, daily_means, arrivals):
    """Whole days fully covered by stock plus visible arrivals (index 0 = today)."""
    for offset, mean in enumerate(daily_means):
        stock += arrivals.get(offset, 0)
        stock -= mean
        if stock < -1e-9:
            return offset
    return len(daily_means)


def detect(
    snapshot: StoreSnapshot,
    forecasts: dict,
    recent_sales: dict,
    spike_forecasts: dict | None = None,
    thresholds: Thresholds = THRESHOLDS,
    open_keys: frozenset = frozenset(),
) -> list[Signal]:
    """Pure detector. forecasts[sku] covers today onward; recent_sales[sku] = completed rows
    (dicts with day, sales_units, ended_without_stock) for the spike window, oldest first;
    spike_forecasts[sku] are ForecastPoints for those same days, made before the window.
    open_keys are dedup keys of currently open alerts (used only for overstock hysteresis)."""
    t, day = thresholds, snapshot.day
    leads = {s.supplier_id: s.lead_time_days for s in snapshot.suppliers}
    products = {p.sku: p for p in snapshot.products}
    stock = {i.sku: i.stock for i in snapshot.inventory}
    signals = []
    cover = {}
    for sku, product in sorted(products.items()):
        fc = forecasts[sku]
        means = [p.mean_units for p in fc.points[: t.horizon_days]]
        lead = leads[product.supplier_id]
        orders = [o for o in snapshot.open_purchase_orders if o.sku == sku]
        arrivals, overdue = {}, []
        for o in orders:
            if o.visible_eta_day < day:
                overdue.append(o.order_id)  # No promised date: do not assume it arrives.
            else:
                offset = o.visible_eta_day - day
                arrivals[offset] = arrivals.get(offset, 0) + o.quantity
        days = covered_days(stock[sku], means, arrivals)
        cover[sku] = days
        base = {
            "snapshot_version": snapshot.snapshot_version,
            "day": day,
            "stock": stock[sku],
            "forecast_source_id": fc.source_id,
            "forecast_warnings": list(fc.warnings),
            "forecast_daily_mean": round(sum(means) / len(means), 3),
            "supplier_id": product.supplier_id,
            "lead_time_days": lead,
            "inbound": [
                {"order_id": o.order_id, "quantity": o.quantity, "eta_day": o.visible_eta_day}
                for o in orders
            ],
        }
        threshold = lead + t.stockout_buffer_days
        if days < threshold and sum(means) > 0:
            signals.append(
                Signal(
                    "stockout_risk",
                    sku,
                    "high" if days < lead else "medium",
                    base
                    | {
                        "covered_days": days,
                        "alert_below_days": threshold,
                        "runs_out_on_day": day + days,
                        "earliest_new_order_arrival_day": day + lead,
                        "excluded_overdue_orders": overdue,
                    },
                )
            )
        position = stock[sku] + sum(o.quantity for o in orders)
        mean = sum(means) / len(means)
        if position >= t.overstock_min_units:
            cover_days = position / mean if mean > 0 else None
            limit = (
                t.overstock_resolve_days
                if f"overstock:{sku}" in open_keys
                else t.overstock_cover_days
            )
            if cover_days is None or cover_days > limit:
                severity = (
                    "medium"
                    if cover_days is not None and cover_days > 2 * t.overstock_cover_days
                    else "low"
                )
                signals.append(
                    Signal(
                        "overstock",
                        sku,
                        severity,
                        base
                        | {
                            "inventory_position": position,
                            "cover_days": round(cover_days, 1) if cover_days else None,
                            "alert_above_days": t.overstock_cover_days,
                            "resolve_below_days": t.overstock_resolve_days,
                            "no_forecast_demand": cover_days is None,
                        },
                    )
                )
        for o in orders:
            original = o.created_day + leads[o.supplier_id]
            moved, late = o.visible_eta_day > original, o.visible_eta_day < day
            if not (moved or late):
                continue
            runs_out_first = (not late) and days < o.visible_eta_day - day
            signals.append(
                Signal(
                    "supplier_delay",
                    sku,
                    "high" if late or runs_out_first else "medium",
                    base
                    | {
                        "reason": "overdue" if late else "eta_moved",
                        "original_eta_day": original,
                        "published_eta_day": o.visible_eta_day,
                        "delay_days": o.visible_eta_day - original,
                        "covered_days_before_arrival": days,
                    },
                    order_id=o.order_id,
                )
            )
    for sku in sorted(spike_forecasts or {}):
        rows = recent_sales.get(sku, [])
        if len(rows) < t.spike_window_days:
            continue
        points = spike_forecasts[sku]
        expected = sum(p.mean_units for p in points)
        band = [p.upper_80_units for p in points]
        upper = sum(band) if None not in band else None
        actual = sum(r["sales_units"] for r in rows)
        extra = actual - expected
        if extra < t.spike_min_extra_units or actual < t.spike_ratio * expected:
            continue
        if t.spike_require_upper_band and upper is not None and actual <= upper:
            continue
        ratio = actual / expected if expected > 0 else None
        censored = [r["day"] for r in rows if r["ended_without_stock"]]
        signals.append(
            Signal(
                "demand_spike",
                sku,
                "high" if ratio is None or ratio >= t.spike_high_ratio else "medium",
                {
                    "snapshot_version": snapshot.snapshot_version,
                    "day": day,
                    "window_days": [r["day"] for r in rows],
                    "actual_units": actual,
                    "expected_units": round(expected, 2),
                    "expected_upper_80_units": round(upper, 2) if upper is not None else None,
                    "ratio": round(ratio, 2) if ratio is not None else None,
                    "stockout_days_in_window": censored,
                    "note": "true demand may be higher: sales were capped by stock"
                    if censored
                    else None,
                    "covered_days": cover[sku],
                },
            )
        )
    return signals


def _day(value) -> date:
    return value if isinstance(value, date) else date.fromisoformat(value)


def history_until(base: SalesHistory, observed_rows, cutoff: date) -> SalesHistory:
    records = [r for r in base.records if r.date <= cutoff]
    records += [
        SalesRecord(
            sku=r["sku"],
            date=_day(r["date"]),
            sales_units=r["sales_units"],
            price_cents=r["price_cents"],
            ended_without_stock=r["ended_without_stock"],
            provenance="simulated",
        )
        for r in observed_rows
        if _day(r["date"]) <= cutoff
    ]
    return SalesHistory(base.tenant_id, tuple(records), f"{base.source_id}+observed:{cutoff}")


def evaluate(
    snapshot,
    base_history,
    observed_rows,
    calendar_origin,
    model_id,
    t=THRESHOLDS,
    open_keys=frozenset(),
):
    """Forecast from public history only, then run the detectors."""
    skus = sorted(p.sku for p in snapshot.products)
    cutoff = calendar_origin + timedelta(days=snapshot.day - 2)  # last completed day
    rows = [r for r in observed_rows if r["day"] < snapshot.day]
    history = history_until(base_history, rows, cutoff)
    forecasts = {
        f.sku: f
        for f in forecast_demand(history, skus, cutoff, horizon=t.horizon_days, model_id=model_id)
    }
    recent, spike = {}, None
    window = t.spike_window_days
    if snapshot.day - 1 >= window:
        first = snapshot.day - window
        recent = {
            sku: sorted(
                (r for r in rows if r["sku"] == sku and r["day"] >= first), key=lambda r: r["day"]
            )
            for sku in skus
        }
        before = cutoff - timedelta(days=window)
        prior = forecast_demand(
            history_until(base_history, rows, before), skus, before, window, model_id
        )
        spike = {f.sku: f.points for f in prior}
    return detect(snapshot, forecasts, recent, spike, t, open_keys)


class Monitor:
    """Processes each committed public snapshot version exactly once, in order."""

    def __init__(
        self,
        dsn,
        tenant,
        base_history: SalesHistory,
        *,
        model_id="weekday_mean_4",
        thresholds=THRESHOLDS,
        failpoint=None,
    ):
        self.dsn, self.tenant, self.base_history = dsn, tenant, base_history
        self.model_id, self.thresholds, self.failpoint = model_id, thresholds, failpoint

    def process_next(self):
        with transaction(self.dsn) as conn:
            store = conn.execute(
                "SELECT calendar_origin FROM app.stores WHERE tenant_id=%s", (self.tenant,)
            ).fetchone()
            if store is None:
                raise ServiceError("forbidden", "store access denied", 403)
            conn.execute(
                "INSERT INTO app.monitor_cursors(tenant_id) VALUES(%s) ON CONFLICT DO NOTHING",
                (self.tenant,),
            )
            cursor = conn.execute(
                "SELECT last_snapshot_version FROM app.monitor_cursors WHERE tenant_id=%s "
                "FOR UPDATE",
                (self.tenant,),
            ).fetchone()["last_snapshot_version"]
            obs = conn.execute(
                "SELECT snapshot_version,day,snapshot FROM app.store_observations "
                "WHERE tenant_id=%s AND snapshot_version>%s ORDER BY snapshot_version LIMIT 1",
                (self.tenant, cursor),
            ).fetchone()
            if obs is None:
                return None
            snapshot = StoreSnapshot.model_validate(obs["snapshot"])
            observed = conn.execute(
                "SELECT day,sku,date,sales_units,price_cents,ended_without_stock "
                "FROM app.observed_sales WHERE tenant_id=%s AND day<%s ORDER BY day,sku",
                (self.tenant, snapshot.day),
            ).fetchall()
            open_keys = frozenset(
                r["dedup_key"]
                for r in conn.execute(
                    "SELECT dedup_key FROM app.alerts WHERE tenant_id=%s AND status='OPEN'",
                    (self.tenant,),
                ).fetchall()
            )
            signals = evaluate(
                snapshot,
                self.base_history,
                observed,
                store["calendar_origin"],
                self.model_id,
                self.thresholds,
                open_keys,
            )
            changes = self._reconcile(conn, snapshot, signals)
            conn.execute(
                "UPDATE app.monitor_cursors SET last_snapshot_version=%s,"
                "updated_at=clock_timestamp() WHERE tenant_id=%s",
                (snapshot.snapshot_version, self.tenant),
            )
            if self.failpoint:
                self.failpoint("before_commit")
        return {
            "snapshot_version": snapshot.snapshot_version,
            "day": snapshot.day,
            "signals": len(signals),
        } | changes

    def run_pending(self, limit=1000):
        results = []
        for _ in range(limit):
            result = self.process_next()
            if result is None:
                break
            results.append(result)
        return results

    def _event(self, conn, alert_id, kind, snapshot, severity, evidence):
        conn.execute(
            "INSERT INTO app.alert_events(tenant_id,alert_id,event_id,kind,day,snapshot_version,"
            "severity,evidence) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                self.tenant,
                alert_id,
                uuid4(),
                kind,
                snapshot.day,
                snapshot.snapshot_version,
                severity,
                Jsonb(evidence),
            ),
        )

    def _reconcile(self, conn, snapshot, signals):
        open_rows = {
            r["dedup_key"]: r
            for r in conn.execute(
                "SELECT alert_id,dedup_key,severity,evidence FROM app.alerts "
                "WHERE tenant_id=%s AND status='OPEN' FOR UPDATE",
                (self.tenant,),
            ).fetchall()
        }
        counts = {"opened": 0, "escalated": 0, "deescalated": 0, "resolved": 0, "unchanged": 0}
        current = {s.dedup_key: s for s in signals}
        for key, signal in sorted(current.items()):
            row = open_rows.get(key)
            if row is None:
                alert_id = uuid4()
                conn.execute(
                    "INSERT INTO app.alerts(tenant_id,alert_id,dedup_key,alert_type,sku,order_id,"
                    "severity,evidence,detector_version,opened_day,opened_snapshot_version,"
                    "last_day,last_snapshot_version) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        self.tenant,
                        alert_id,
                        key,
                        signal.alert_type,
                        signal.sku,
                        signal.order_id,
                        signal.severity,
                        Jsonb(signal.evidence),
                        DETECTOR_VERSION,
                        snapshot.day,
                        snapshot.snapshot_version,
                        snapshot.day,
                        snapshot.snapshot_version,
                    ),
                )
                self._event(conn, alert_id, "opened", snapshot, signal.severity, signal.evidence)
                counts["opened"] += 1
                continue
            change = SEVERITY_RANK[signal.severity] - SEVERITY_RANK[row["severity"]]
            conn.execute(
                "UPDATE app.alerts SET severity=%s,evidence=%s,last_day=%s,"
                "last_snapshot_version=%s,updated_at=clock_timestamp() "
                "WHERE tenant_id=%s AND alert_id=%s",
                (
                    signal.severity,
                    Jsonb(signal.evidence),
                    snapshot.day,
                    snapshot.snapshot_version,
                    self.tenant,
                    row["alert_id"],
                ),
            )
            if change:
                kind = "escalated" if change > 0 else "deescalated"
                self._event(conn, row["alert_id"], kind, snapshot, signal.severity, signal.evidence)
                counts[kind] += 1
            else:
                counts["unchanged"] += 1
        for key, row in sorted(open_rows.items()):
            if key in current:
                continue
            conn.execute(
                "UPDATE app.alerts SET status='RESOLVED',resolved_day=%s,"
                "resolved_snapshot_version=%s,updated_at=clock_timestamp() "
                "WHERE tenant_id=%s AND alert_id=%s",
                (snapshot.day, snapshot.snapshot_version, self.tenant, row["alert_id"]),
            )
            self._event(
                conn,
                row["alert_id"],
                "resolved",
                snapshot,
                row["severity"],
                {
                    "snapshot_version": snapshot.snapshot_version,
                    "day": snapshot.day,
                    "last_evidence": row["evidence"],
                },
            )
            counts["resolved"] += 1
        return counts


def thresholds_record(t: Thresholds = THRESHOLDS) -> dict:
    return {"detector_version": DETECTOR_VERSION} | asdict(t)
