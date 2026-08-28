-- 005: make the free trial the default plan, and repair the users who were created
-- unable to generate anything.
--
-- THE BUG: /users/register granted 20 credits, while migration 003 defaulted
-- users.plan_name to 'basic' — and Basic is 30 images x 1 credit = 30 credits per job.
-- 20 < 30, so EVERY registered user hit "402 Insufficient credits" on their first
-- generation, permanently: there is no billing flow to top credits up. Every real
-- signup in the users table is currently in this dead state.
--
-- THE FIX: new users default to the 'trial' plan (10 images = 10 credits), which the
-- 20-credit grant covers twice over. Paid plans (basic/pro/expert) are entered by
-- purchase, which is what grants the credits to afford them.
--
-- NOTE: GO batch separators are REQUIRED (see 003/004).

-- Point the column default at the trial plan for all future inserts. (register_user
-- now also sets plan_name explicitly, so this is belt-and-braces.)
IF EXISTS (SELECT 1 FROM sys.default_constraints WHERE name = 'DF_users_plan_name')
    ALTER TABLE dbo.users DROP CONSTRAINT DF_users_plan_name;
GO

ALTER TABLE dbo.users ADD CONSTRAINT DF_users_plan_name
    DEFAULT 'trial' FOR plan_name;
GO

-- Repair existing accounts. Anyone on 'basic' today got there from the OLD column
-- default, not from a purchase — there is no billing code that can set a paid plan, so
-- 'basic' here is unambiguously "never paid". Move them to the trial plan so their
-- existing credits actually buy a session.
--
-- Deliberately does NOT touch 'pro'/'expert'/'monthly' rows; only the stranded default.
UPDATE dbo.users
SET plan_name = 'trial'
WHERE plan_name = 'basic';
GO

-- Anyone left with fewer credits than one trial session can't generate at all; top the
-- stranded accounts back up to the standard grant. Never REDUCES anyone's credits.
UPDATE dbo.users
SET credits_remaining = 20
WHERE plan_name = 'trial' AND (credits_remaining IS NULL OR credits_remaining < 10);
GO

-- ── Verify ────────────────────────────────────────────────────────────────
-- Every row should now be able to afford one job on its plan:
--   SELECT user_id, plan_name, credits_remaining FROM dbo.users ORDER BY plan_name;
--   -- trial rows need >= 10, pro needs >= 50, expert >= 65.

-- ── Rollback ──────────────────────────────────────────────────────────────
-- ALTER TABLE dbo.users DROP CONSTRAINT DF_users_plan_name;
-- ALTER TABLE dbo.users ADD CONSTRAINT DF_users_plan_name DEFAULT 'basic' FOR plan_name;
-- UPDATE dbo.users SET plan_name = 'basic' WHERE plan_name = 'trial';
-- (Rolling back re-creates the 20-credits-vs-30-cost deadlock. Only do this together
--  with reverting shared/plans.py.)
