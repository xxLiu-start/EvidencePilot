-- Run as the database/schema owner after migrations have created the schema.
-- Set a strong password interactively with: \password evidencepilot_runtime
CREATE ROLE evidencepilot_runtime LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
GRANT CONNECT ON DATABASE evidencepilot TO evidencepilot_runtime;
GRANT USAGE ON SCHEMA public TO evidencepilot_runtime;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO evidencepilot_runtime;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO evidencepilot_runtime;

-- Ensure future migration-created tables are usable by the runtime role.
ALTER DEFAULT PRIVILEGES IN SCHEMA public
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO evidencepilot_runtime;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
GRANT USAGE, SELECT ON SEQUENCES TO evidencepilot_runtime;
