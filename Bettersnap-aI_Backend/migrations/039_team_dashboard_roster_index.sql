-- 039: Support bounded, ordered Teams dashboard roster and invitation reads.

IF NOT EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE name = 'IX_org_members_dashboard_roster'
      AND object_id = OBJECT_ID('dbo.organization_members')
)
    CREATE INDEX IX_org_members_dashboard_roster
        ON dbo.organization_members (organization_id, status, joined_at)
        INCLUDE (membership_id, user_id, invitation_id, credits_granted, credits_remaining);
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE name = 'IX_invitations_dashboard_pending'
      AND object_id = OBJECT_ID('dbo.invitations')
)
    CREATE INDEX IX_invitations_dashboard_pending
        ON dbo.invitations (organization_id, status, created_at)
        INCLUDE (invitation_id, email, expires_at);
GO
