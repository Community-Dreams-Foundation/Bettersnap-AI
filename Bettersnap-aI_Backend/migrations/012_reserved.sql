-- Migration number 012 was intentionally unused in the original sequence.
-- Keep this tracked no-op marker so migration numbering remains contiguous and
-- a future schema change cannot be inserted behind already-deployed migrations.
SELECT 1 AS migration_012_reserved;
