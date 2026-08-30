-- 035: organization_branding — org-level look-and-feel settings for Teams.
--
-- Audit finding T-013: "Team brand settings are browser-local only." TeamSetup
-- collects style, use case, attire, background and per-member limits, but nothing
-- persists them, so configuration is lost across devices and cannot govern what
-- members actually generate.
--
-- One row per organization (organization_id is the PK, not just an FK), because
-- Phase 1 has a single brand per org. If multiple brand sets are ever needed
-- (Doc 02 Phase 2 mentions this), add a surrogate key and an is_active flag then —
-- a one-row-per-org table is the cheapest thing to widen later.
--
-- ENFORCEMENT IS NOT DECIDED YET. `enforcement_mode` records the intent so the
-- generation path can honour it once product answers: does an org's brand CONSTRAIN
-- what members may pick, or merely PRE-SELECT defaults they can override? Storing it
-- now means no second migration when that answer arrives. Until then the API reads
-- and writes these values and the job path ignores them.
--
-- Attire and background hold catalog REFS (the "category:id" strings that
-- shared/catalog.py validates via valid_attire_ref / valid_background_ref), NOT free
-- text. They are validated in the API, not by a CHECK constraint, because the catalog
-- lives in code and moves independently of the schema.
--
-- Idempotent and safe to re-run. GO separators required (see 003).

IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE object_id = OBJECT_ID('dbo.organization_branding'))
    CREATE TABLE dbo.organization_branding (
        organization_id     UNIQUEIDENTIFIER NOT NULL PRIMARY KEY,
        -- Catalog refs, e.g. 'business:navy_suit'. NULL = not set, which is distinct
        -- from "set to nothing" and lets the UI show an unconfigured state.
        default_attire_ref  VARCHAR(128)     NULL,
        default_background_ref VARCHAR(128)  NULL,
        -- Free-form product selection strings the setup wizard already collects.
        style_key           VARCHAR(64)      NULL,
        use_case_key        VARCHAR(64)      NULL,
        -- Per-member ceiling on images, independent of credits. NULL = no extra limit
        -- beyond the member's own credit balance. Not a substitute for credits: the
        -- credit check still runs first.
        max_images_per_member INT            NULL,
        -- 'default'  — members see these pre-selected but may change them
        -- 'enforce'  — members may not deviate
        -- Product decision pending; 'default' is the safe initial value because it
        -- cannot block a member from generating.
        enforcement_mode    VARCHAR(16)      NOT NULL
                            CONSTRAINT DF_org_branding_enforcement DEFAULT 'default',
        updated_by_user_id  UNIQUEIDENTIFIER NULL,   -- Entra oid of the admin who last saved
        created_at          DATETIME2        NOT NULL
                            CONSTRAINT DF_org_branding_created_at DEFAULT SYSUTCDATETIME(),
        updated_at          DATETIME2        NOT NULL
                            CONSTRAINT DF_org_branding_updated_at DEFAULT SYSUTCDATETIME(),
        CONSTRAINT FK_org_branding_org FOREIGN KEY (organization_id)
            REFERENCES dbo.organizations (organization_id)
    );
GO

-- Guard the vocabulary at the DB level so a typo in application code can't park a row
-- in a mode the generation path doesn't recognise.
IF NOT EXISTS (SELECT 1 FROM sys.check_constraints WHERE name = 'CK_org_branding_enforcement')
    ALTER TABLE dbo.organization_branding ADD CONSTRAINT CK_org_branding_enforcement
        CHECK (enforcement_mode IN ('default', 'enforce'));
GO

-- Non-negative, and 0 would mean "cannot generate", which should be expressed by
-- suspending the member rather than by a limit of zero.
IF NOT EXISTS (SELECT 1 FROM sys.check_constraints WHERE name = 'CK_org_branding_max_images')
    ALTER TABLE dbo.organization_branding ADD CONSTRAINT CK_org_branding_max_images
        CHECK (max_images_per_member IS NULL OR max_images_per_member > 0);
GO

-- ── Verify ────────────────────────────────────────────────────────────────
--   SELECT COUNT(*) FROM dbo.organization_branding;   -- exists, 0 rows
--
-- ── Rollback ──────────────────────────────────────────────────────────────
--   DROP TABLE dbo.organization_branding;
