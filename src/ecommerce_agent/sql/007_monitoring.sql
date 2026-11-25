-- Phase 3A: public observation outbox, monitor cursor, and durable alerts.
-- The monitor reads only released store state and completed sales; never evaluator truth.
ALTER TABLE authn.service_bindings DROP CONSTRAINT service_bindings_kind_check;
ALTER TABLE authn.service_bindings ADD CONSTRAINT service_bindings_kind_check
    CHECK (kind IN ('agent','executor','simulator','monitor'));

-- Every committed public snapshot version, written by trigger in the same transaction as the
-- store change, so a crashed or late monitor can never skip an observation.
CREATE TABLE app.store_observations (
    tenant_id text NOT NULL REFERENCES app.stores, snapshot_version integer NOT NULL,
    day integer NOT NULL CHECK(day>0), snapshot jsonb NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(tenant_id,snapshot_version)
);
CREATE FUNCTION app.record_observation() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,app AS $$
BEGIN
    INSERT INTO app.store_observations(tenant_id,snapshot_version,day,snapshot)
    VALUES(NEW.tenant_id,(NEW.snapshot->>'snapshot_version')::integer,
           (NEW.snapshot->>'day')::integer,NEW.snapshot)
    ON CONFLICT DO NOTHING;
    RETURN NULL;
END $$;
CREATE TRIGGER record_observation AFTER INSERT OR UPDATE OF snapshot ON app.stores
    FOR EACH ROW EXECUTE FUNCTION app.record_observation();
INSERT INTO app.store_observations(tenant_id,snapshot_version,day,snapshot)
SELECT tenant_id,(snapshot->>'snapshot_version')::integer,(snapshot->>'day')::integer,snapshot
FROM app.stores ON CONFLICT DO NOTHING;

CREATE TABLE app.monitor_cursors (
    tenant_id text PRIMARY KEY REFERENCES app.stores,
    last_snapshot_version integer NOT NULL DEFAULT 0,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

-- One row per issue. At most one OPEN alert per (tenant, dedup_key); repeats update it.
CREATE TABLE app.alerts (
    tenant_id text NOT NULL REFERENCES app.stores, alert_id uuid NOT NULL,
    dedup_key text NOT NULL,
    alert_type text NOT NULL CHECK(alert_type IN
        ('stockout_risk','supplier_delay','demand_spike','overstock')),
    sku text NOT NULL, order_id text,
    severity text NOT NULL CHECK(severity IN ('low','medium','high')),
    status text NOT NULL DEFAULT 'OPEN' CHECK(status IN ('OPEN','RESOLVED')),
    evidence jsonb NOT NULL, detector_version text NOT NULL,
    opened_day integer NOT NULL, opened_snapshot_version integer NOT NULL,
    last_day integer NOT NULL, last_snapshot_version integer NOT NULL,
    resolved_day integer, resolved_snapshot_version integer,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(tenant_id,alert_id),
    CHECK((status='RESOLVED')=(resolved_day IS NOT NULL))
);
CREATE UNIQUE INDEX alerts_one_open ON app.alerts(tenant_id,dedup_key) WHERE status='OPEN';
CREATE TABLE app.alert_events (
    tenant_id text NOT NULL, alert_id uuid NOT NULL, event_id uuid NOT NULL,
    kind text NOT NULL CHECK(kind IN ('opened','escalated','deescalated','resolved')),
    day integer NOT NULL, snapshot_version integer NOT NULL,
    severity text NOT NULL, evidence jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(tenant_id,event_id),
    FOREIGN KEY(tenant_id,alert_id) REFERENCES app.alerts
);
CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON app.alert_events
    FOR EACH ROW EXECUTE FUNCTION app.immutable_record();
CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON app.store_observations
    FOR EACH ROW EXECUTE FUNCTION app.immutable_record();

DO $$ DECLARE t text; BEGIN
    FOREACH t IN ARRAY ARRAY['store_observations','monitor_cursors','alerts','alert_events'] LOOP
        EXECUTE format('ALTER TABLE app.%I ENABLE ROW LEVEL SECURITY',t);
        EXECUTE format('ALTER TABLE app.%I FORCE ROW LEVEL SECURITY',t);
        EXECUTE format('CREATE POLICY tenant_read ON app.%I FOR SELECT USING (authn.can_access(tenant_id))',t);
        EXECUTE format('CREATE POLICY owner_all ON app.%I TO ec_owner USING(true) WITH CHECK(true)',t);
        EXECUTE format('REVOKE ALL ON app.%I FROM PUBLIC',t);
    END LOOP;
END $$;
CREATE POLICY monitor_cursor_insert ON app.monitor_cursors FOR INSERT TO ec_monitor
    WITH CHECK(authn.can_access(tenant_id));
CREATE POLICY monitor_cursor_update ON app.monitor_cursors FOR UPDATE TO ec_monitor
    USING(authn.can_access(tenant_id)) WITH CHECK(authn.can_access(tenant_id));
CREATE POLICY monitor_alert_insert ON app.alerts FOR INSERT TO ec_monitor
    WITH CHECK(authn.can_access(tenant_id));
CREATE POLICY monitor_alert_update ON app.alerts FOR UPDATE TO ec_monitor
    USING(authn.can_access(tenant_id)) WITH CHECK(authn.can_access(tenant_id));
CREATE POLICY monitor_event_insert ON app.alert_events FOR INSERT TO ec_monitor
    WITH CHECK(authn.can_access(tenant_id));

GRANT USAGE ON SCHEMA app,authn TO ec_monitor;
GRANT EXECUTE ON FUNCTION authn.session_identity(),authn.member_role(text),authn.can_access(text)
    TO ec_monitor;
GRANT SELECT ON app.stores,app.observed_sales,app.policy_versions,app.store_observations,
    app.monitor_cursors,app.alerts,app.alert_events TO ec_monitor;
GRANT INSERT,UPDATE(last_snapshot_version,updated_at) ON app.monitor_cursors TO ec_monitor;
GRANT INSERT ON app.alerts,app.alert_events TO ec_monitor;
GRANT UPDATE(severity,status,evidence,last_day,last_snapshot_version,resolved_day,
    resolved_snapshot_version,updated_at) ON app.alerts TO ec_monitor;
GRANT SELECT ON app.store_observations,app.alerts,app.alert_events,app.monitor_cursors
    TO ec_api,ec_agent;
