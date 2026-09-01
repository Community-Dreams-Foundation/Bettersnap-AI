-- Bounded customer dashboard and paginated history both read a user's newest jobs.
-- Cover the response fields so SQL Server can satisfy that ordered lookup without
-- scanning the full jobs table or repeatedly looking up the base row.
IF NOT EXISTS (
    SELECT 1
    FROM sys.indexes
    WHERE name = 'IX_jobs_user_created_dashboard'
      AND object_id = OBJECT_ID('dbo.jobs')
)
BEGIN
    CREATE INDEX IX_jobs_user_created_dashboard
        ON dbo.jobs (user_id, created_at DESC)
        INCLUDE (job_id, status, job_type, category, output_blob_path, completed_at, job_params);
END;
