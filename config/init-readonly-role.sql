-- The read-only role behind the SQL tool.
--
-- This is the guarantee. The sqlglot parsing, the forbidden-keyword check and the forced LIMIT in
-- tools/warehouse.py are defence in depth on top of it — useful, but a prompt or a parser can be
-- defeated and a missing GRANT cannot. If a prompt injection produces a DELETE that slips past
-- every application-layer check, this role rejects it at the database.

CREATE ROLE sentinel_readonly LOGIN PASSWORD 'sentinel_readonly';

GRANT CONNECT ON DATABASE sentinel TO sentinel_readonly;
GRANT USAGE ON SCHEMA public TO sentinel_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO sentinel_readonly;

-- Tables created later inherit the same grant, so a new table is not accidentally unreadable
-- (or, worse, writable).
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO sentinel_readonly;

ALTER ROLE sentinel_readonly SET default_transaction_read_only = on;
ALTER ROLE sentinel_readonly SET statement_timeout = '30s';
