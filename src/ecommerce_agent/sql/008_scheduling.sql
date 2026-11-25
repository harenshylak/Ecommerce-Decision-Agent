-- Phase 3B: store mode and controller, scheduled investigation jobs, review-before-advance gate.
ALTER TABLE authn.service_bindings DROP CONSTRAINT service_bindings_kind_check;
ALTER TABLE authn.service_bindings ADD CONSTRAINT service_bindings_kind_check
    CHECK (kind IN ('agent','executor','simulator','monitor','scheduler'));

-- manual: legacy merchant-question flow only. demo: scheduled reviews plus merchant questions.
-- evaluation: scheduled reviews only, so every controller gets identical action opportunities.
ALTER TABLE app.stores
    ADD COLUMN mode text NOT NULL DEFAULT 'manual' CHECK(mode IN ('manual','demo','evaluation')),
    ADD COLUMN controller text NOT NULL DEFAULT 'fixed'
        CHECK(controller IN ('fixed','single_agent','multi_agent'));

ALTER TABLE app.investigations
    ADD COLUMN source text NOT NULL DEFAULT 'merchant' CHECK(source IN ('scheduled','merchant')),
    ADD COLUMN controller text,
    ADD COLUMN review_day integer CHECK(review_day>0),
    ADD COLUMN lease_owner uuid, ADD COLUMN lease_expires_at timestamptz,
    ADD COLUMN heartbeat_at timestamptz,
    ADD COLUMN attempts integer NOT NULL DEFAULT 0,
    ADD COLUMN max_attempts integer NOT NULL DEFAULT 3,
    ADD COLUMN ready_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    ADD COLUMN last_error text, ADD COLUMN outcome jsonb,
    ADD COLUMN usage jsonb NOT NULL DEFAULT '{}',
    ADD COLUMN updated_at timestamptz NOT NULL DEFAULT clock_timestamp();
UPDATE app.investigations i SET controller=s.controller FROM app.stores s
    WHERE s.tenant_id=i.tenant_id AND i.controller IS NULL;
ALTER TABLE app.investigations ALTER COLUMN controller SET NOT NULL;
ALTER TABLE app.investigations ADD CHECK(state IN
    ('QUEUED','LEASED','PROPOSED','NO_OP','ESCALATED','TIMED_OUT','FAILED'));
ALTER TABLE app.investigations ADD CHECK((source='scheduled')=(review_day IS NOT NULL));
-- At most one scheduled review per store and review day, however often the scheduler runs.
CREATE UNIQUE INDEX investigations_one_review ON app.investigations(tenant_id,review_day)
    WHERE source='scheduled';

-- The store's controller is stamped on each investigation and frozen once work exists.
CREATE FUNCTION app.stamp_controller() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,app AS $$
BEGIN
    SELECT controller INTO NEW.controller FROM app.stores WHERE tenant_id=NEW.tenant_id;
    RETURN NEW;
END $$;
CREATE TRIGGER stamp_controller BEFORE INSERT ON app.investigations
    FOR EACH ROW EXECUTE FUNCTION app.stamp_controller();
CREATE FUNCTION app.freeze_controller() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,app AS $$
BEGIN
    IF (NEW.controller,NEW.mode) IS DISTINCT FROM (OLD.controller,OLD.mode)
       AND EXISTS(SELECT 1 FROM app.investigations WHERE tenant_id=NEW.tenant_id) THEN
        RAISE EXCEPTION 'controller and mode are frozen once investigations exist'
            USING ERRCODE='42501';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER freeze_controller BEFORE UPDATE OF controller,mode ON app.stores
    FOR EACH ROW EXECUTE FUNCTION app.freeze_controller();

-- Merchant questions only where the store mode allows them.
DROP POLICY write_investigations ON app.investigations;
CREATE POLICY write_investigations ON app.investigations FOR INSERT TO ec_api
    WITH CHECK(authn.is_approver(tenant_id) AND source='merchant' AND state='QUEUED'
        AND attempts=0 AND review_day IS NULL AND EXISTS(
            SELECT 1 FROM app.stores s WHERE s.tenant_id=investigations.tenant_id
            AND s.mode IN ('manual','demo')));
-- The scheduler may only queue a fresh review for the store's current simulated day.
CREATE POLICY schedule_review ON app.investigations FOR INSERT TO ec_scheduler
    WITH CHECK(authn.can_access(tenant_id) AND source='scheduled' AND state='QUEUED'
        AND attempts=0 AND lease_owner IS NULL AND outcome IS NULL AND usage='{}'::jsonb
        AND EXISTS(SELECT 1 FROM app.stores s WHERE s.tenant_id=investigations.tenant_id
            AND s.mode<>'manual' AND (s.snapshot->>'day')::integer=review_day));

-- Non-null reason means the simulated day must not advance yet.
CREATE FUNCTION app.review_blocker(t text) RETURNS text
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,app,authn AS $$
DECLARE store app.stores; cfg jsonb; today integer;
BEGIN
    IF NOT authn.can_access(t) THEN
        RAISE EXCEPTION 'store access denied' USING ERRCODE='42501';
    END IF;
    SELECT * INTO store FROM app.stores WHERE tenant_id=t;
    IF store.mode='manual' THEN RETURN NULL; END IF;
    IF EXISTS(SELECT 1 FROM app.investigations WHERE tenant_id=t
              AND state IN ('QUEUED','LEASED')) THEN
        RETURN 'review_in_progress';
    END IF;
    SELECT config INTO cfg FROM app.policy_versions
        WHERE tenant_id=t AND version=store.policy_version;
    today := (store.snapshot->>'day')::integer;
    IF cfg->'simulation'->'review_days' @> to_jsonb(today)
       AND NOT EXISTS(SELECT 1 FROM app.investigations WHERE tenant_id=t
                      AND source='scheduled' AND review_day=today) THEN
        RETURN 'review_not_run';
    END IF;
    RETURN NULL;
END $$;

REVOKE ALL ON FUNCTION app.review_blocker(text) FROM PUBLIC;
GRANT USAGE ON SCHEMA app,authn TO ec_scheduler;
GRANT EXECUTE ON FUNCTION authn.session_identity(),authn.member_role(text),authn.can_access(text),
    authn.lock_store(text) TO ec_scheduler;
GRANT EXECUTE ON FUNCTION app.review_blocker(text) TO ec_api,ec_simulator,ec_scheduler,ec_agent;
GRANT SELECT ON app.stores,app.policy_versions,app.alerts,app.monitor_cursors,
    app.store_observations,app.investigations TO ec_scheduler;
GRANT INSERT ON app.investigations,app.audit_events TO ec_scheduler;
GRANT UPDATE(state,lease_owner,lease_expires_at,heartbeat_at,attempts,ready_at,last_error,
    outcome,usage,updated_at) ON app.investigations TO ec_agent;
CREATE POLICY scheduler_audit ON app.audit_events FOR INSERT TO ec_scheduler
    WITH CHECK(authn.can_access(tenant_id));
