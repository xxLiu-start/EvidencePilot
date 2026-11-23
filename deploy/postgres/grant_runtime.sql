\set ON_ERROR_STOP on

SELECT format('GRANT CONNECT ON DATABASE %I TO evidencepilot_app', current_database()) \gexec
SELECT format('GRANT CONNECT ON DATABASE %I TO evidencepilot_dispatcher', current_database()) \gexec

GRANT USAGE ON SCHEMA public TO evidencepilot_app, evidencepilot_dispatcher;
REVOKE CREATE ON SCHEMA public FROM PUBLIC, evidencepilot_app, evidencepilot_dispatcher;

GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO evidencepilot_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO evidencepilot_app;
REVOKE UPDATE, DELETE ON TABLE public.audit_events FROM evidencepilot_app;

-- The dispatcher is a trusted control-plane process. BYPASSRLS is narrowly
-- paired with table-level privileges on the outbox only; it cannot read tasks,
-- documents, claims, evidence, memberships, or user records.
GRANT SELECT, UPDATE ON TABLE public.outbox_events TO evidencepilot_dispatcher;

ALTER DEFAULT PRIVILEGES FOR ROLE :"migration_role" IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO evidencepilot_app;
ALTER DEFAULT PRIVILEGES FOR ROLE :"migration_role" IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO evidencepilot_app;
