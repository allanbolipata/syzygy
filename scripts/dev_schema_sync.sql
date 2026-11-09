-- Dev-DB schema sync for PR #19 (DMP two-tier, commit e2c6dfe).
-- main.py uses create_all, which adds missing TABLES but never ALTERs an
-- existing table — so a dev DB created before the models change keeps the
-- old pairing_table shape. Apply once:
--   set -a && source .env && set +a
--   psql "${DATABASE_URL/postgresql+psycopg2/postgresql}" -f scripts/dev_schema_sync.sql
--
-- 1) multi_pair: the fan-out flag (one latest row per matched normal under
--    the DMP logic; exempt from the one-latest-row-per-tumor constraint).
ALTER TABLE pairing_table
    ADD COLUMN IF NOT EXISTS multi_pair BOOLEAN NOT NULL DEFAULT FALSE;

-- 2) The tumor-scoped unique index predates the PR but its partial-index
--    predicate changed: old "WHERE latest" would make DMP fan-out rows
--    collide (one tumor, many latest normals). Recreate with the new
--    predicate that exempts multi_pair rows.
DROP INDEX IF EXISTS uq_pairing_table_tumor_pairing_logic_latest;
CREATE UNIQUE INDEX IF NOT EXISTS uq_pairing_table_tumor_pairing_logic_latest
    ON pairing_table (tumor_id, pairing_logic_id)
    WHERE latest AND COALESCE(multi_pair, FALSE) = FALSE;

-- 3) Pair-scoped unique index (idempotent; already present on DBs from the
--    DMP chain, created here for older ones).
CREATE UNIQUE INDEX IF NOT EXISTS uq_pairing_table_tumor_normal_pairing_logic_latest
    ON pairing_table (tumor_id, normal_id, pairing_logic_id)
    WHERE latest;
