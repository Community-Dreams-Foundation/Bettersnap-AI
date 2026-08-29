-- 032: Allow the payment-gated Teams workspace state on existing databases.
--
-- Migration 022 added CK_org_status only when the constraint did not already exist.
-- Databases created from the original Teams schema can therefore still have an older
-- constraint that rejects the API's intentional initial status, 'pending_payment'.
-- Replace the constraint in a forward-only, idempotent migration so workspace creation
-- works without changing existing organization rows.

IF OBJECT_ID('dbo.organizations', 'U') IS NOT NULL
BEGIN
    IF EXISTS (
        SELECT 1
        FROM sys.check_constraints
        WHERE name = 'CK_org_status'
          AND parent_object_id = OBJECT_ID('dbo.organizations')
    )
        ALTER TABLE dbo.organizations DROP CONSTRAINT CK_org_status;

    ALTER TABLE dbo.organizations ADD CONSTRAINT CK_org_status
        CHECK (status IN ('pending_payment', 'active', 'suspended', 'closed'));
END;
GO
