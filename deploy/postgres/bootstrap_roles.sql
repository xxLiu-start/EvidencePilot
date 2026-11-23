\set ON_ERROR_STOP on

SELECT format(
    'CREATE ROLE evidencepilot_app LOGIN PASSWORD %L NOSUPERUSER NOBYPASSRLS NOINHERIT',
    :'app_password'
)
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'evidencepilot_app')
\gexec

ALTER ROLE evidencepilot_app WITH LOGIN PASSWORD :'app_password' NOSUPERUSER NOBYPASSRLS NOINHERIT;

SELECT format(
    'CREATE ROLE evidencepilot_dispatcher LOGIN PASSWORD %L NOSUPERUSER BYPASSRLS NOINHERIT',
    :'dispatcher_password'
)
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'evidencepilot_dispatcher')
\gexec

ALTER ROLE evidencepilot_dispatcher WITH LOGIN PASSWORD :'dispatcher_password' NOSUPERUSER BYPASSRLS NOINHERIT;
