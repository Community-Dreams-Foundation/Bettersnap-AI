-- 035: Teams pricing quotes, checkout attempts and fulfilment snapshots (`teams_basic_v1`).
--
-- SAFE TO RESTRUCTURE IN PLACE: this migration has never been applied anywhere. The
-- production ledger ends at 032, and 033/034 are also still pending. Layering a
-- corrective 036 on top of an unapplied 035 would ship two files describing one change.
--
-- WHY THIS EXISTS. Three separate holes, all in the gap between "the customer was
-- quoted a price" and "the server granted entitlement":
--
--  1. A QUOTE THAT LIVES ONLY IN THE CLIENT IS NOT A QUOTE. Returning a generated UUID
--     to the browser and accepting it back proves nothing — there is nothing to look up,
--     so ownership, expiry and single-use cannot be checked. dbo.teams_quotes makes a
--     quote a durable server record that is validated and CONSUMED exactly once.
--
--  2. ONE PAID SESSION IS NOT ONE PAYABLE SESSION. A filtered unique index on
--     status='paid' stops the second webhook granting credits, but by then the customer
--     may already have been charged twice — two open Stripe Sessions are two payable
--     pages. dbo.organization_live_checkout makes "at most one LIVE attempt per
--     organization" a primary-key invariant, so a double-click cannot produce a second
--     payable session in the first place.
--
--  3. A STRIPE SESSION MUST NOT EXIST WITHOUT A SERVER RECORD. Creating the Stripe
--     Session first and inserting the row afterwards means a crash in between leaves a
--     payable session nothing knows about. Attempts are therefore inserted as 'creating'
--     and COMMITTED before Stripe is called, then moved to 'pending' with the session id.
--     A stranded 'creating' row is recoverable: the deterministic idempotency key makes
--     re-calling Stripe return the SAME session rather than a second one.
--
-- APPEND-ONLY AND GUARDED. New tables plus NULLABLE columns on existing ones. No
-- existing row is read, rewritten or deleted. Safe to run on a live database and safe to
-- run twice. GO separators are REQUIRED (see 003's note).

-- ── 1. organizations.updated_at ───────────────────────────────────────────
-- 022 declares this column, but production was created from the ORIGINAL Features_team
-- Teams schema (also why 032 had to replace CK_org_status), so it can be genuinely
-- absent. NOT NULL with a DEFAULT backfills as a metadata-only operation.
IF OBJECT_ID('dbo.organizations', 'U') IS NOT NULL
   AND COL_LENGTH('dbo.organizations', 'updated_at') IS NULL
    ALTER TABLE dbo.organizations
        ADD updated_at DATETIME2 NOT NULL
            CONSTRAINT DF_org_updated_at_035 DEFAULT SYSUTCDATETIME();
GO

-- ── 2. teams_quotes ───────────────────────────────────────────────────────
-- The authoritative record of a price this server issued. Checkout loads it under a
-- lock and verifies owner, organization, expiry, status and contract version before any
-- money can move, then consumes it in the same transaction that reserves the attempt.
IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE object_id = OBJECT_ID('dbo.teams_quotes'))
    CREATE TABLE dbo.teams_quotes (
        quote_id          UNIQUEIDENTIFIER NOT NULL PRIMARY KEY,
        -- The authenticated caller the quote was issued to. A quote is NOT bearer
        -- currency: another user presenting it is a mismatch, not a purchase.
        user_id           UNIQUEIDENTIFIER NOT NULL,
        -- NULL while the admin is pricing before the workspace exists. Once set, the
        -- quote may only be spent on THAT organization.
        organization_id   UNIQUEIDENTIFIER NULL,
        seats             INT              NOT NULL,
        total_cents       INT              NOT NULL,
        credits_per_seat  INT              NOT NULL,
        pricing_version   VARCHAR(64)      NOT NULL,
        plan_id           VARCHAR(64)      NOT NULL,
        currency          VARCHAR(8)       NOT NULL
                              CONSTRAINT DF_tq_currency DEFAULT 'usd',
        breakdown_json    NVARCHAR(MAX)    NULL,
        issued_at         DATETIME2        NOT NULL
                              CONSTRAINT DF_tq_issued DEFAULT SYSUTCDATETIME(),
        expires_at        DATETIME2        NOT NULL,
        status            VARCHAR(16)      NOT NULL
                              CONSTRAINT DF_tq_status DEFAULT 'issued',
        consumed_at       DATETIME2        NULL,
        -- Which attempt spent it. Lets a stranded 'creating' attempt be RECOVERED by its
        -- own quote without that reading as a reuse of someone else's quote.
        consumed_by_attempt UNIQUEIDENTIFIER NULL,
        CONSTRAINT CK_tq_status CHECK (status IN ('issued', 'consumed', 'expired')),
        CONSTRAINT CK_tq_seats CHECK (seats > 0),
        CONSTRAINT CK_tq_total CHECK (total_cents > 0)
    );
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_tq_user'
               AND object_id = OBJECT_ID('dbo.teams_quotes'))
    CREATE INDEX IX_tq_user ON dbo.teams_quotes (user_id, issued_at DESC);
GO

-- ── 3. organization_checkout_sessions ─────────────────────────────────────
-- One row per checkout ATTEMPT. Keyed on a server-generated attempt_id, NOT on the
-- Stripe session id, precisely because the row must exist BEFORE Stripe is called and
-- therefore before a session id exists.
--
-- IMMUTABLE BY CONVENTION: everything except status / checkout_session_id /
-- fulfilled_at is written once. The snapshot's whole value is that it cannot drift.
IF NOT EXISTS (SELECT 1 FROM sys.tables
               WHERE object_id = OBJECT_ID('dbo.organization_checkout_sessions'))
    CREATE TABLE dbo.organization_checkout_sessions (
        attempt_id           UNIQUEIDENTIFIER NOT NULL PRIMARY KEY,
        -- NULL only while status='creating'. Set once Stripe returns.
        checkout_session_id  VARCHAR(255)     NULL,
        -- Stripe's hosted page for this attempt. Stored so a repeat request can RETURN
        -- THE SAME payable page instead of creating a second one.
        checkout_url         NVARCHAR(1000)   NULL,
        organization_id      UNIQUEIDENTIFIER NOT NULL,
        quote_id             UNIQUEIDENTIFIER NOT NULL,
        -- Deterministic, derived from organization + quote. Replaying it at Stripe
        -- returns the SAME session instead of creating a second payable one.
        idempotency_key      VARCHAR(255)     NOT NULL,
        pricing_version      VARCHAR(64)      NOT NULL,
        plan_id              VARCHAR(64)      NOT NULL,
        -- Seats this attempt is FOR. Held here rather than written onto the
        -- organization: opening a checkout is not a purchase, and a cancelled attempt
        -- must leave no trace on the workspace.
        seats                INT              NOT NULL,
        credits_per_seat     INT              NOT NULL,
        expected_total_cents INT              NOT NULL,
        currency             VARCHAR(8)       NOT NULL
                                 CONSTRAINT DF_orgcs_currency DEFAULT 'usd',
        breakdown_json       NVARCHAR(MAX)    NULL,
        status               VARCHAR(16)      NOT NULL
                                 CONSTRAINT DF_orgcs_status DEFAULT 'creating',
        -- The expiration we ASKED Stripe for, mirrored locally. Diagnostic and
        -- reconciliation aid only: the local slot is released by Stripe's
        -- checkout.session.expired event, NEVER by comparing this to the clock, because
        -- a session Stripe still considers open must stay claimed.
        expires_at           DATETIME2        NULL,
        created_by_user_id   UNIQUEIDENTIFIER NOT NULL,
        created_at           DATETIME2        NOT NULL
                                 CONSTRAINT DF_orgcs_created_at DEFAULT SYSUTCDATETIME(),
        settled_at           DATETIME2        NULL,
        fulfilled_at         DATETIME2        NULL,
        CONSTRAINT FK_orgcs_org FOREIGN KEY (organization_id)
            REFERENCES dbo.organizations (organization_id),
        CONSTRAINT FK_orgcs_quote FOREIGN KEY (quote_id)
            REFERENCES dbo.teams_quotes (quote_id),
        CONSTRAINT CK_orgcs_status
            CHECK (status IN ('creating', 'pending', 'paid', 'failed', 'expired', 'cancelled')),
        -- A live attempt may lack a session id; anything settled must have one, except a
        -- 'creating' attempt abandoned before Stripe ever answered.
        CONSTRAINT CK_orgcs_session_id
            CHECK (checkout_session_id IS NOT NULL
                   OR status IN ('creating', 'cancelled', 'expired', 'failed')),
        CONSTRAINT CK_orgcs_seats CHECK (seats > 0),
        CONSTRAINT CK_orgcs_total CHECK (expected_total_cents > 0),
        CONSTRAINT CK_orgcs_credits CHECK (credits_per_seat > 0)
    );
GO

-- Webhook fulfilment looks an attempt up by the Stripe session id.
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'UQ_orgcs_session'
               AND object_id = OBJECT_ID('dbo.organization_checkout_sessions'))
    CREATE UNIQUE INDEX UQ_orgcs_session
        ON dbo.organization_checkout_sessions (checkout_session_id)
        WHERE checkout_session_id IS NOT NULL;
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_orgcs_org_created'
               AND object_id = OBJECT_ID('dbo.organization_checkout_sessions'))
    CREATE INDEX IX_orgcs_org_created
        ON dbo.organization_checkout_sessions (organization_id, created_at DESC);
GO

-- AT MOST ONE PAID attempt per organization, enforced by the database. A Teams purchase
-- is one-time; a second successful checkout is a double charge.
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'UQ_orgcs_one_paid_per_org'
               AND object_id = OBJECT_ID('dbo.organization_checkout_sessions'))
    CREATE UNIQUE INDEX UQ_orgcs_one_paid_per_org
        ON dbo.organization_checkout_sessions (organization_id)
        WHERE status = 'paid';
GO

-- ── 4. organization_live_checkout ─────────────────────────────────────────
-- At most one LIVE ('creating' or 'pending') attempt per organization, as a PRIMARY KEY
-- rather than a filtered index — SQL Server filtered-index predicates cannot express
-- "status IN ('creating','pending')" (no OR), and this invariant is too important to
-- leave to application logic. The row is inserted with the attempt and DELETED the
-- moment the attempt settles, so a cancelled or expired attempt frees the organization
-- immediately and reversibly.
IF NOT EXISTS (SELECT 1 FROM sys.tables
               WHERE object_id = OBJECT_ID('dbo.organization_live_checkout'))
    CREATE TABLE dbo.organization_live_checkout (
        organization_id UNIQUEIDENTIFIER NOT NULL PRIMARY KEY,
        attempt_id      UNIQUEIDENTIFIER NOT NULL,
        created_at      DATETIME2        NOT NULL
                            CONSTRAINT DF_orglc_created DEFAULT SYSUTCDATETIME(),
        CONSTRAINT FK_orglc_org FOREIGN KEY (organization_id)
            REFERENCES dbo.organizations (organization_id),
        CONSTRAINT FK_orglc_attempt FOREIGN KEY (attempt_id)
            REFERENCES dbo.organization_checkout_sessions (attempt_id)
    );
GO

-- ── 5. organization_legacy_checkout_allowlist ─────────────────────────────
-- BOUNDED EVIDENCE that a snapshot-less Stripe session is a genuine pre-035 checkout.
--
-- Without this, "no snapshot" means "fulfil unconditionally", which would accept ANY
-- signed org session — including one created outside this server. Signed metadata is not
-- proof of provenance: it proves Stripe sent it, not that we authorised it.
--
-- Rows are IMPORTED DELIBERATELY from a read-only Stripe inventory taken before rollout
-- (see the migration notes below). An empty table is the correct default: it means every
-- snapshot-less session fails closed.
IF NOT EXISTS (SELECT 1 FROM sys.tables
               WHERE object_id = OBJECT_ID('dbo.organization_legacy_checkout_allowlist'))
    CREATE TABLE dbo.organization_legacy_checkout_allowlist (
        checkout_session_id VARCHAR(255)     NOT NULL PRIMARY KEY,
        organization_id     UNIQUEIDENTIFIER NOT NULL,
        -- Stripe's own creation timestamp for the session, recorded at inventory time
        -- from the Stripe API — independent of anything the webhook payload claims.
        stripe_created_at   DATETIME2        NOT NULL,
        expected_total_cents INT             NULL,
        recorded_by         NVARCHAR(256)    NOT NULL,
        recorded_at         DATETIME2        NOT NULL
                                CONSTRAINT DF_orglegacy_recorded DEFAULT SYSUTCDATETIME(),
        note                NVARCHAR(500)    NULL,
        -- Reconciled ONCE. A second delivery finds status='consumed' and grants nothing.
        status              VARCHAR(16)      NOT NULL
                                CONSTRAINT DF_orglegacy_status DEFAULT 'open',
        consumed_at         DATETIME2        NULL,
        CONSTRAINT FK_orglegacy_org FOREIGN KEY (organization_id)
            REFERENCES dbo.organizations (organization_id),
        CONSTRAINT CK_orglegacy_status CHECK (status IN ('open', 'consumed', 'void'))
    );
GO

-- ── 6. organization_payments: what the payment was FOR ────────────────────
-- All NULLABLE: historical rows keep NULL.
IF COL_LENGTH('dbo.organization_payments', 'checkout_session_id') IS NULL
    ALTER TABLE dbo.organization_payments ADD checkout_session_id VARCHAR(255) NULL;
GO

IF COL_LENGTH('dbo.organization_payments', 'pricing_version') IS NULL
    ALTER TABLE dbo.organization_payments ADD pricing_version VARCHAR(64) NULL;
GO

IF COL_LENGTH('dbo.organization_payments', 'seats_paid') IS NULL
    ALTER TABLE dbo.organization_payments ADD seats_paid INT NULL;
GO

IF COL_LENGTH('dbo.organization_payments', 'credits_per_seat') IS NULL
    ALTER TABLE dbo.organization_payments ADD credits_per_seat INT NULL;
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_orgpay_session'
               AND object_id = OBJECT_ID('dbo.organization_payments'))
    CREATE INDEX IX_orgpay_session
        ON dbo.organization_payments (checkout_session_id)
        WHERE checkout_session_id IS NOT NULL;
GO

-- ── Verify ────────────────────────────────────────────────────────────────
--   SELECT COUNT(*) FROM dbo.teams_quotes;                            -- 0
--   SELECT COUNT(*) FROM dbo.organization_checkout_sessions;          -- 0
--   SELECT COUNT(*) FROM dbo.organization_live_checkout;              -- 0
--   SELECT COUNT(*) FROM dbo.organization_legacy_checkout_allowlist;  -- 0 (fails closed)
--   SELECT COL_LENGTH('dbo.organizations','updated_at');              -- non-NULL
--
-- ── REQUIRED BEFORE ROLLOUT: read-only production inventory ───────────────
-- NOT performed by this migration and NOT yet performed at all. Before enabling Teams
-- checkout, take a READ-ONLY inventory and import the result into the allowlist:
--   1. Stripe (read-only, live mode): list Checkout Sessions with
--      metadata[payment_type]='org_seats' whose status is 'open' or 'complete' and whose
--      payment_status is not 'paid' — i.e. still capable of being paid. Record each
--      session's id, `created` (Stripe's own timestamp) and amount_total.
--   2. SQL (read-only): for each, confirm the organization exists and is still
--      'pending_payment', and that organization_payments holds no row for it.
--   3. Import ONLY sessions satisfying both into this allowlist, with recorded_by set to
--      the operator and note describing the inventory run.
--   4. Anything not imported fails closed at fulfilment and is reconciled by hand.
-- Sessions created AFTER the cutoff (TEAMS_SNAPSHOT_CUTOFF_UTC) must never be imported:
-- they are required to carry a snapshot.
--
-- ── Rollback ──────────────────────────────────────────────────────────────
--   DROP TABLE dbo.organization_legacy_checkout_allowlist;
--   DROP TABLE dbo.organization_live_checkout;          -- FK to checkout_sessions
--   DROP INDEX IX_orgpay_session ON dbo.organization_payments;
--   ALTER TABLE dbo.organization_payments
--       DROP COLUMN checkout_session_id, pricing_version, seats_paid, credits_per_seat;
--   DROP TABLE dbo.organization_checkout_sessions;      -- FK to teams_quotes
--   DROP TABLE dbo.teams_quotes;
--   -- organizations.updated_at is intentionally NOT dropped: 022 declares it as part of
--   -- the baseline schema, so removing it would diverge from the intended shape.
