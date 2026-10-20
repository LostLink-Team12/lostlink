-- Development credentials only. Run against the local Compose database.
-- Existing roles and credentials are deliberately preserved.
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'lostlink_app') THEN
        CREATE ROLE lostlink_app LOGIN PASSWORD 'local-development-only'
            NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
    END IF;
END
$$;
GRANT CONNECT ON DATABASE lostlink TO lostlink_app;
