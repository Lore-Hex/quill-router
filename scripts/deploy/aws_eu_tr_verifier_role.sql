-- tr_verifier: the Postgres role clickhouse.verify_postgres_delivery connects
-- as on the AWS-EU ClickHouse node. Reviewed operator step, run by hand as the
-- DSQL admin; scripts/deploy/aws_eu_postgres_delivery_verifier_install.sh
-- prints the exact command and then proves the result from the node.
--
-- Least privilege: tr_entities holds every entity kind (keys, wallets, users),
-- so the role gets SELECT on a view of generation rows only, projected to the
-- fields the verifier compares, and nothing on any table. Every statement is
-- safe to re-run, and a re-run repairs a drifted view: it is dropped and
-- created again (CREATE OR REPLACE cannot remove columns), and the GRANT that
-- the drop removed is reapplied. psql's default autocommit gives each schema
-- change its own transaction, as DSQL requires. Needs psql (\gexec) and the
-- node role ARN:
--
--   psql "host=... dbname=postgres user=admin sslmode=require" \
--     -v node_role_arn=arn:aws:iam::<account>:role/<node role> \
--     -f scripts/deploy/aws_eu_tr_verifier_role.sql
\set ON_ERROR_STOP on

DROP VIEW IF EXISTS tr_verifier_generations;

CREATE VIEW tr_verifier_generations AS
SELECT id,
       body ->> 'total_cost_microdollars' AS total_cost_microdollars,
       body ->> 'tokens_prompt' AS tokens_prompt,
       body ->> 'tokens_completion' AS tokens_completion,
       body ->> 'cached_input_tokens' AS cached_input_tokens,
       body ->> 'reasoning_tokens' AS reasoning_tokens,
       body ->> 'model' AS model,
       body ->> 'provider' AS provider,
       body ->> 'status' AS status,
       body ->> 'created_at' AS created_at,
       updated_at
FROM tr_entities
WHERE kind = 'generation';

SELECT 'CREATE ROLE tr_verifier WITH LOGIN'
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tr_verifier') \gexec

SELECT format('AWS IAM GRANT tr_verifier TO %L', :'node_role_arn')
WHERE NOT EXISTS (
  SELECT 1 FROM sys.iam_pg_role_mappings
  WHERE pg_role_name = 'tr_verifier' AND arn = :'node_role_arn'
) \gexec

GRANT SELECT ON tr_verifier_generations TO tr_verifier;
