-- Development credentials only. Run against the local Compose database.
-- Existing roles and credentials are deliberately preserved.
-- Each service uses separate non-owner runtime roles; migrations run as the Compose admin.
DO $$
DECLARE
    role_name text;
BEGIN
    FOREACH role_name IN ARRAY ARRAY[
        'lostlink_app',
        'lostlink_venue_api', 'lostlink_venue_platform', 'lostlink_venue_worker',
        'lostlink_venue_identity',
        'lostlink_item_api', 'lostlink_item_internal', 'lostlink_item_worker',
        'lostlink_matching_api', 'lostlink_matching_internal', 'lostlink_matching_worker',
        'lostlink_claim_api', 'lostlink_claim_worker',
        'lostlink_notification_api', 'lostlink_notification_admin',
        'lostlink_notification_worker',
        'lostlink_delivery_api', 'lostlink_delivery_worker'
    ] LOOP
        IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = role_name) THEN
            EXECUTE format(
                'CREATE ROLE %I LOGIN PASSWORD %L '
                'NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS',
                role_name,
                'local-development-only'
            );
        END IF;
        EXECUTE format('GRANT CONNECT ON DATABASE lostlink TO %I', role_name);
    END LOOP;
END
$$;
