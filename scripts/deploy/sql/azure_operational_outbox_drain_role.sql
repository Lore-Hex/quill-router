-- The Azure drain's login: SELECT and DELETE on ONE table, and nothing else.
--
-- WHY NOT THE ADMIN
-- -----------------
-- tradmin can read every table in this database, including tr_entities, which
-- holds raw member emails and workspace ids — the identifiers
-- analytics_surrogate() exists to keep OFF the analytics host. The drain runs
-- on the ClickHouse node. A node holding admin credentials for the operational
-- database is a node that can read the customer list, and it would work
-- perfectly, which is the problem: nothing would ever ask.
--
-- scripts/deploy/azure_clickhouse_drain_install.sh refuses to install the unit
-- unless the role it is given can SELECT and DELETE this table and CANNOT read
-- tr_entities. All three are measured against the database, not against the
-- intention of whoever ran this file.
--
-- WHY DELETE IS NOT OPTIONAL
-- --------------------------
-- The drain writes ClickHouse and only then deletes. A role with SELECT and no
-- DELETE produces the quietest possible failure: every row is delivered, every
-- metric reads healthy, rows_per_second is positive — and the outbox grows
-- forever because nothing is ever removed. The installer checks for it by name.
--
-- HOW THE PASSWORD GETS IN HERE
-- -----------------------------
-- It is NOT in this file and must never be. `:'drain_password'` is a psql
-- variable, set by the caller through a pipe:
--
--   {
--     printf "\\set drain_password '"
--     az keyvault secret show --vault-name tr-azure-analytics-kv \
--       -n drain-postgres-password --query value -o tsv | tr -d '\n'
--     printf "'\n"
--     cat scripts/deploy/sql/azure_operational_outbox_drain_role.sql
--   } | psql "host=tr-azure-pg.postgres.database.azure.com port=5432 \
--             user=tradmin dbname=trustedrouter sslmode=require" \
--         -v ON_ERROR_STOP=1 -f -
--
-- Through a PIPE and not `-v drain_password=...`, because a psql -v value is
-- argv and every local user can read argv out of ps. Not through a temporary
-- file either: the file is what gets committed by accident.
--
-- The value itself is generated once, into Key Vault, by
-- scripts/deploy/azure_clickhouse.sh — which never reads it back. Nobody has to
-- invent a password and nobody has to know one.
--
-- Azure Flexible Server is PASSWORD auth: there is no DSQL-style IAM token
-- here, so the drain's environment file supplies PGPASSWORD and its DSN
-- carries no password at all (a DSN that carries one is refused outright by
-- PostgresOperationalOutboxSource, because argv is readable).
--
-- Re-runnable: CREATE ROLE on an existing role is an error, so creation is
-- conditional (via \gexec) and the password is then set unconditionally. Every
-- GRANT below is idempotent on its own.
--
-- NOT written as a DO $$ ... $$ block: psql does NOT expand :'variables'
-- inside a dollar-quoted string, so a DO block would try to set the password to
-- the literal text `:'drain_password'` — and would succeed, leaving a role
-- whose password is a nine-character string nobody knows they know.

\set ON_ERROR_STOP on

SELECT 'CREATE ROLE tr_drain WITH LOGIN'
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tr_drain')
\gexec

ALTER ROLE tr_drain WITH LOGIN PASSWORD :'drain_password';

-- The role attributes are ASSERTED at the bottom rather than set here.
-- CREATE ROLE already defaults to NOSUPERUSER / NOCREATEDB / NOCREATEROLE, and
-- on Azure Flexible Server `tradmin` is not a superuser: an
-- `ALTER ROLE ... NOSUPERUSER NOBYPASSRLS` line would abort this whole file
-- with a permission error while looking like extra safety.

GRANT CONNECT ON DATABASE trustedrouter TO tr_drain;
GRANT USAGE ON SCHEMA public TO tr_drain;
GRANT SELECT, DELETE ON TABLE tr_operational_analytics_outbox TO tr_drain;

-- Deliberately NOT granted, and each omission is load-bearing:
--   * no GRANT on any other table. The drain reads one queue;
--   * no ALTER DEFAULT PRIVILEGES. A future table must not become readable
--     here by existing;
--   * no CREATE on the schema. The drain never writes DDL;
--   * no sequence grants. It inserts nothing.

-- Say what was actually granted, so the operator reads the database's answer
-- rather than this file's intention.
SELECT
    r.rolname AS role,
    r.rolsuper AS is_superuser_MUST_BE_FALSE,
    r.rolcreatedb AS can_create_db_MUST_BE_FALSE,
    r.rolcreaterole AS can_create_role_MUST_BE_FALSE,
    has_table_privilege('tr_drain', 'tr_operational_analytics_outbox', 'SELECT') AS can_select_outbox,
    has_table_privilege('tr_drain', 'tr_operational_analytics_outbox', 'DELETE') AS can_delete_outbox,
    has_table_privilege('tr_drain', 'tr_entities', 'SELECT') AS can_read_entities_MUST_BE_FALSE
FROM pg_roles r
WHERE r.rolname = 'tr_drain';
