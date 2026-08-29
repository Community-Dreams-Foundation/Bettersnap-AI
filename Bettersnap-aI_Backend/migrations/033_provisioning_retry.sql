-- 033: Bounded retry state for ACA PRE-CONTAINER replica-provisioning failures.
--
-- WHAT THIS IS FOR
-- When Azure fails to back a GPU replica BEFORE the container starts, the execution ends
-- terminal with NO ContainerStarted, NO container exit code and NO preflight blob. That is an
-- infrastructure fault, not an application failure, and it is the one class worth re-dispatching.
-- Retrying needs durable per-row state: how many attempts have been consumed, which ACA
-- executions were already counted (so a duplicate reconcile cannot consume a second attempt),
-- and when the row was FIRST observed terminal.
--
-- WHY first_terminal_observed_at IS PERSISTED
-- ACA can report a terminal status with end_time = NULL, and Log Analytics ingestion lags by
-- ~90s. Age therefore cannot be recomputed from scratch on every reaper pass: with a NULL
-- end_time it would be 0 forever and the row would never leave the ingestion grace window, so a
-- job could stay pending indefinitely. Stamping the first terminal observation ONCE makes age
-- monotonic across passes, which is what guarantees the bounded observation window always
-- expires and every row reaches a terminal decision.
--
-- RUNTIME INVARIANT — THE STAMP IS PER ACA EXECUTION ATTEMPT, NOT PER ROW
-- first_terminal_observed_at measures how long THIS execution attempt has been terminal. A
-- retry starts a NEW attempt, so the clock must restart. Every successful retry transition
-- MUST, in the SAME transaction that sets status='queued' and external_execution_id=NULL, also
-- set first_terminal_observed_at=NULL:
--
--     UPDATE dbo.jobs
--        SET status = 'queued',
--            external_execution_id = NULL,
--            first_terminal_observed_at = NULL,      -- reset: new attempt, new clock
--            provisioning_attempts = ?, provisioning_execution_ids = ?
--      WHERE job_id = ? AND status = 'dispatching';
--
-- Leaving it set would carry the previous attempt's age into the retry, so the second attempt
-- would appear to have already exhausted MAX_OBSERVATION the moment it was dispatched and would
-- be refunded as unclassified before Log Analytics could describe it. Same rule for
-- dbo.lora_trainings on the training retry path.
--
-- Forward-only and restart-safe: every statement is guarded, so re-running is a no-op. Adding
-- these columns changes no existing row's behaviour (attempts default 0 = "never retried").

------------------------------------------------------------------------------
-- dbo.jobs — generation / fused generation retry state
------------------------------------------------------------------------------
IF COL_LENGTH('dbo.jobs', 'provisioning_attempts') IS NULL
    ALTER TABLE dbo.jobs ADD provisioning_attempts INT NOT NULL
        CONSTRAINT DF_jobs_provisioning_attempts DEFAULT 0 WITH VALUES;
GO

-- Append-only list of ACA execution ids already counted against this row. A reconcile pass that
-- sees an execution id already present must NOT consume another attempt (duplicate dispatch).
IF COL_LENGTH('dbo.jobs', 'provisioning_execution_ids') IS NULL
    ALTER TABLE dbo.jobs ADD provisioning_execution_ids NVARCHAR(MAX) NULL;
GO

-- Stamped once, by the first pass that observes a terminal ACA status. Never updated after.
IF COL_LENGTH('dbo.jobs', 'first_terminal_observed_at') IS NULL
    ALTER TABLE dbo.jobs ADD first_terminal_observed_at DATETIME2 NULL;
GO

------------------------------------------------------------------------------
-- dbo.lora_trainings — training / fused train_infer retry state
------------------------------------------------------------------------------
IF COL_LENGTH('dbo.lora_trainings', 'provisioning_attempts') IS NULL
    ALTER TABLE dbo.lora_trainings ADD provisioning_attempts INT NOT NULL
        CONSTRAINT DF_lora_trainings_provisioning_attempts DEFAULT 0 WITH VALUES;
GO

IF COL_LENGTH('dbo.lora_trainings', 'provisioning_execution_ids') IS NULL
    ALTER TABLE dbo.lora_trainings ADD provisioning_execution_ids NVARCHAR(MAX) NULL;
GO

IF COL_LENGTH('dbo.lora_trainings', 'first_terminal_observed_at') IS NULL
    ALTER TABLE dbo.lora_trainings ADD first_terminal_observed_at DATETIME2 NULL;
GO
