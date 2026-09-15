-- Sentinel-RAG's own database and app-facing login role.
--
-- Not staged into pg-init automatically: postgres is now shared, global infra (see CLAUDE.md,
-- "The infra layer: postgres, redis, Portainer"), so its databases are created on demand, the
-- same way astronomy_db was for otel-demo, rather than assumed to exist on first boot.
-- Run this by hand (`psql -U postgres -f config/init-sentinel-db.sql`) once Sentinel-RAG actually
-- needs its database, then re-stage and run config/init-readonly-role.sql, which grants against
-- the database created here.

CREATE USER sentinelapp WITH PASSWORD 'sentinel';
CREATE DATABASE sentinel OWNER sentinelapp;
