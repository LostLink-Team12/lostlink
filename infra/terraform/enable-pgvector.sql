-- Run once against the lostlink DB with the RDS admin identity, from a network
-- that can reach private RDS (normally the C3 migration Job in EKS).
-- Application roles must not use the RDS master credentials.
CREATE EXTENSION IF NOT EXISTS vector;
SELECT extversion FROM pg_extension WHERE extname = 'vector';
