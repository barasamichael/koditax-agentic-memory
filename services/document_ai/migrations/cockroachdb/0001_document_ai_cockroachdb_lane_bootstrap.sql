-- Document AI CockroachDB migration lane bootstrap.
--
-- This migration intentionally makes no business-schema changes yet. It only
-- establishes the deterministic lane and durable migration identity boundary
-- for future Document AI CockroachDB migrations.
SELECT 1;

