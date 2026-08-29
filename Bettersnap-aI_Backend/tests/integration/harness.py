"""Connections, migrations and seeding for the isolated SQL Server suite.

The migrations run here are the REAL files, applied by the REAL runner functions from
scripts/run_migrations.py. Nothing is copied or re-expressed: if a migration is wrong, this
harness is wrong in the same way, which is the point.

Seeded states are documented against whether production can genuinely reach them. States that
production CANNOT reach are marked DRIFT and are created without ever disabling or bypassing a
constraint -- see REACHABILITY below.

REACHABILITY (audited against the actual FKs, not assumed)
----------------------------------------------------------
  FK_jobs_user        jobs.user_id            -> users.user_id     EXISTS
  FK_credit_tx_user   credit_transactions     -> users.user_id     EXISTS
  FK_credit_tx_job    credit_transactions     -> jobs.job_id       EXISTS
  organization_members.user_id                -> (none)            NO FK
  lora_trainings.user_id                      -> (none)            NO FK

Consequences the plan originally got wrong:

  * "delete the users row, then refund the job" is IMPOSSIBLE. FK_jobs_user blocks deleting a
    user who has any job, and FK_credit_tx_user blocks deleting one who has any ledger row.
    A personal job can therefore never lose its refund target.
  * The genuinely reachable missing-refund-target is an ORGANIZATION one:
    organization_members has NO FK to users, and removing a member is an ordinary product
    operation. That is what cases 8 and 12 use.
  * A FREE training CAN outlive its user (no FK on lora_trainings.user_id, no jobs, no ledger
    rows) -- reachable.
  * A PAID training outliving its user is NOT reachable: the retrain_charge ledger row's FK
    blocks the delete. It is seeded as an explicit DRIFT fixture by deleting the ledger row
    first, which is itself the drift being modelled. No constraint is ever disabled.
"""
import os
import sys
import uuid

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)
SCRIPTS_DIR = os.path.join(BACKEND_DIR, "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from tests.integration import guardrails                      # noqa: E402

# Bounded so no test can hang, but generous enough that a CONTENDED case is decided by the
# lock manager rather than by a stopwatch. A test that "passes" because four contenders timed
# out has proven nothing about serialization, so these are set well above the time any of
# these short transactions needs.
QUERY_TIMEOUT_S = 60
LOCK_TIMEOUT_MS = 30000

# How a seeded state came to exist. Anything not REACHABLE must say so in its own name.
REACHABLE = "reachable"
DRIFT = "schema-drift"


def normalize_filter(definition):
    """`([fused_job_id] IS NOT NULL)` -> `fused_job_id IS NOT NULL`.

    Same normalisation migration 034 performs in its own guard, so the harness and the
    migration agree on what "the right filter" means."""
    if definition is None:
        return None
    text = str(definition).strip()
    while text.startswith("(") and text.endswith(")"):
        text = text[1:-1].strip()
    return text.replace("[", "").replace("]", "").strip()


def same_guid(left, right):
    """Are these two values the SAME UNIQUEIDENTIFIER?

    SQL Server renders uniqueidentifier UPPERCASE through pyodbc; `Harness.new_id()` mints
    `str(uuid.uuid4())`, which is lowercase. Comparing an id that came back from a query
    against one the harness minted therefore needs parsing, exactly as production's
    `provisioning_retry.same_user_id` does.

    Deliberately a SEPARATE helper: `same_user_id` is documented as user-id-only so it can
    never be applied to ACA execution names, which are opaque strings and must keep comparing
    exactly. This one is for test-side uniqueidentifier comparisons (job ids, org ids).

    Fails closed on None, malformed and non-UUID values.
    """
    try:
        return uuid.UUID(str(left)) == uuid.UUID(str(right))
    except (AttributeError, TypeError, ValueError):
        return False


class Harness:
    def __init__(self, host="127.0.0.1", port=guardrails.REQUIRED_PORT,
                 database=guardrails.REQUIRED_DATABASE):
        self.host, self.port, self.database = guardrails.check_target(host, port, database)
        self._password = guardrails.read_password()
        self._connections = []
        self.run_id = uuid.uuid4().hex[:12]

    # -- identity -------------------------------------------------------
    def new_id(self):
        """A fresh UNIQUEIDENTIFIER, unique per run so a partial cleanup cannot collide."""
        return str(uuid.uuid4())

    def label(self, what):
        return "%s-%s" % (what, self.run_id)

    # -- connections ----------------------------------------------------
    def connect(self):
        """A real, separate connection. Autocommit OFF so tests own their transactions."""
        import pyodbc
        conn = pyodbc.connect(
            guardrails.connection_string(self.host, self.port, self.database,
                                         self._password),
            autocommit=False, timeout=QUERY_TIMEOUT_S)
        conn.timeout = QUERY_TIMEOUT_S
        cur = conn.cursor()
        cur.execute("SET LOCK_TIMEOUT %d" % LOCK_TIMEOUT_MS)
        # DEADLOCK_PRIORITY LOW is deliberately NOT set: a deadlock here is a real finding
        # about the locking design, and every case treats one as a failure.
        conn.commit()
        self._connections.append(conn)
        return conn

    def close_all(self):
        while self._connections:
            conn = self._connections.pop()
            try:
                conn.rollback()
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass

    # -- schema ---------------------------------------------------------
    def reset_schema(self, conn):
        """Drop EVERY object so migrations run against a genuinely empty database.

        Order matters: FKs first, then tables. Only ever runs against the validated
        bettersnap_test database -- guardrails.check_target has already refused anything else.
        """
        cur = conn.cursor()
        cur.execute("""
            DECLARE @sql NVARCHAR(MAX) = N'';
            SELECT @sql += N'ALTER TABLE ' + QUOTENAME(SCHEMA_NAME(t.schema_id))
                        + N'.' + QUOTENAME(t.name)
                        + N' DROP CONSTRAINT ' + QUOTENAME(fk.name) + N';'
            FROM sys.foreign_keys fk
            JOIN sys.tables t ON t.object_id = fk.parent_object_id;
            EXEC sp_executesql @sql;
        """)
        cur.execute("""
            DECLARE @sql NVARCHAR(MAX) = N'';
            SELECT @sql += N'DROP TABLE ' + QUOTENAME(SCHEMA_NAME(schema_id))
                        + N'.' + QUOTENAME(name) + N';'
            FROM sys.tables;
            EXEC sp_executesql @sql;
        """)
        conn.commit()

    def apply_all_migrations(self, conn, upto=None):
        """Run the REAL migration files through the REAL runner. Returns applied filenames.

        The tracking table comes FIRST, from run_migrations._TRACKING_DDL — the runner's own
        constant, not a copy. `apply_migration` records each file with
        an INSERT against dbo.schema_migrations, and `run_migrations.main()` creates that table
        before its loop; calling `apply_migration` directly (as this harness does, to avoid
        main()'s CLI and connection handling) skips that step, so 000_baseline applied its DDL
        and then failed to record itself. The DDL is guarded by `IF NOT EXISTS`, so re-running
        setup is a no-op.
        """
        import run_migrations
        cur = conn.cursor()
        cur.execute(run_migrations._TRACKING_DDL)
        conn.commit()
        applied = []
        for path in run_migrations._migration_files():
            name = os.path.basename(path)
            if upto is not None and name > upto:
                continue
            with open(path, encoding="utf-8") as fh:
                sql = fh.read()
            run_migrations.apply_migration(conn, cur, name, sql)
            applied.append(name)
        return applied

    def verify_runtime_schema(self, conn):
        import run_migrations
        return run_migrations.verify_runtime_schema(conn.cursor())

    def fused_link_shape(self, conn):
        """The EXACT shape of 034's FK and index, for restoration verification.

        Mirrors what migration 034's own guards check, so a restore that leaves anything
        different is caught here rather than by the next case behaving strangely.
        """
        cur = conn.cursor()
        cur.execute("""
            SELECT fk.is_disabled, fk.is_not_trusted,
                   fk.delete_referential_action_desc, fk.update_referential_action_desc,
                   (SELECT COUNT(*) FROM sys.foreign_key_columns c
                     WHERE c.constraint_object_id = fk.object_id)              AS col_pairs,
                   COL_NAME(fkc.parent_object_id, fkc.parent_column_id)        AS parent_col,
                   OBJECT_NAME(fk.referenced_object_id)                        AS ref_table,
                   COL_NAME(fkc.referenced_object_id, fkc.referenced_column_id) AS ref_col
            FROM sys.foreign_keys fk
            JOIN sys.foreign_key_columns fkc ON fkc.constraint_object_id = fk.object_id
            WHERE fk.name = 'FK_lora_trainings_fused_job'
              AND fk.parent_object_id = OBJECT_ID('dbo.lora_trainings')
        """)
        rows = cur.fetchall()
        fk = None
        if rows:
            r = rows[0]
            fk = {"is_disabled": bool(r[0]), "is_not_trusted": bool(r[1]),
                  "delete_action": r[2], "update_action": r[3], "col_pairs": int(r[4]),
                  "parent_col": r[5], "ref_table": r[6], "ref_col": r[7],
                  "row_count": len(rows)}

        cur.execute("""
            SELECT i.is_unique, i.has_filter, i.filter_definition, i.is_disabled,
                   (SELECT COUNT(*) FROM sys.index_columns ic
                     WHERE ic.object_id = i.object_id AND ic.index_id = i.index_id
                       AND ic.is_included_column = 0)                          AS key_cols,
                   (SELECT COUNT(*) FROM sys.index_columns ic
                     WHERE ic.object_id = i.object_id AND ic.index_id = i.index_id
                       AND ic.is_included_column = 1)                          AS included_cols,
                   (SELECT TOP 1 COL_NAME(ic.object_id, ic.column_id)
                      FROM sys.index_columns ic
                     WHERE ic.object_id = i.object_id AND ic.index_id = i.index_id
                       AND ic.is_included_column = 0)                          AS key_col
            FROM sys.indexes i
            WHERE i.object_id = OBJECT_ID('dbo.lora_trainings')
              AND i.name = 'UX_lora_trainings_fused_job'
        """)
        row = cur.fetchone()
        ix = None
        if row:
            ix = {"is_unique": bool(row[0]), "has_filter": bool(row[1]),
                  "filter": normalize_filter(row[2]), "is_disabled": bool(row[3]),
                  "key_cols": int(row[4]), "included_cols": int(row[5]),
                  "key_col": row[6]}
        return {"fk": fk, "index": ix}

    # -- introspection ---------------------------------------------------
    def fk(self, conn, name):
        """(exists, is_disabled, is_not_trusted) for a foreign key."""
        cur = conn.cursor()
        cur.execute(
            "SELECT is_disabled, is_not_trusted FROM sys.foreign_keys WHERE name = ?", name)
        row = cur.fetchone()
        return (row is not None, bool(row[0]) if row else None,
                bool(row[1]) if row else None)

    def index(self, conn, table, name):
        cur = conn.cursor()
        cur.execute(
            "SELECT is_unique, has_filter, filter_definition, is_disabled "
            "FROM sys.indexes WHERE object_id = OBJECT_ID(?) AND name = ?", table, name)
        row = cur.fetchone()
        return None if row is None else {
            "is_unique": bool(row[0]), "has_filter": bool(row[1]),
            "filter_definition": row[2], "is_disabled": bool(row[3])}

    # -- seeding ---------------------------------------------------------
    def seed_user(self, conn, *, credits=0, monthly=0, one_time=0,
                  subscription_type="monthly", lora_status="ready", plan_name="monthly_pro"):
        """A users row. REACHABLE: this is what /users/register creates."""
        user_id = self.new_id()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO users (user_id, email, credits_remaining, "
            "monthly_credits_remaining, one_time_credits_remaining, subscription_type, "
            "lora_status, plan_name) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            user_id, "%s@example.invalid" % self.label("t"), credits, monthly, one_time,
            subscription_type, lora_status, plan_name)
        return user_id

    def seed_job(self, conn, user_id, *, status="processing", credit_cost=40,
                 monthly_cost=None, one_time_cost=None, source_type="monthly",
                 organization_id=None, execution_id=None, with_reserve=True):
        """A jobs row plus its job_reserve ledger row.

        REACHABLE: reserve_job_slot writes exactly this pair in one transaction. The reserve
        row is seeded by default because build_refund_plan now REQUIRES it for bucketed and
        organization-funded jobs.
        """
        import json
        from shared import credit_ledger
        job_id = self.new_id()
        params = {"credit_cost": credit_cost}
        if source_type == "monthly":
            params["monthly_credit_cost"] = (credit_cost if monthly_cost is None
                                             else monthly_cost)
            params["one_time_credit_cost"] = (0 if one_time_cost is None else one_time_cost)
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO jobs (job_id, user_id, status, job_params, source_type, "
            "organization_id, external_execution_id, created_at, dispatched_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, GETUTCDATE(), GETUTCDATE())",
            job_id, user_id, status, json.dumps(params), source_type, organization_id,
            execution_id)
        if with_reserve:
            credit_ledger.record(cur, user_id, -credit_cost,
                                 credit_ledger.REASON_JOB_RESERVE, job_id)
        return job_id

    def seed_training(self, conn, user_id, *, status="training", monthly_cost=0,
                      one_time_cost=0, execution_id=None, fused_job_id=None):
        """REACHABLE: reserve_training_slot writes this row."""
        training_id = self.new_id()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO lora_trainings (training_id, user_id, status, files_json, "
            "class_word, source_type, monthly_credit_cost, one_time_credit_cost, "
            "external_execution_id, fused_job_id, created_at) "
            "VALUES (?, ?, ?, '[]', 'woman', 'monthly', ?, ?, ?, ?, GETUTCDATE())",
            training_id, user_id, status, monthly_cost, one_time_cost, execution_id,
            fused_job_id)
        return training_id

    def seed_org_membership(self, conn, user_id, *, credits=0):
        """An organization + membership. REACHABLE: the Teams invite/accept flow."""
        org_id = self.new_id()
        cur = conn.cursor()
        # admin_user_id, NOT owner_user_id -- migration 022 names it admin_user_id ("Entra
        # oid; matches users.user_id"). credits_per_seat, created_at and updated_at all carry
        # real DEFAULTs in 022, so only the four NOT-NULL-without-default columns are supplied.
        cur.execute(
            "INSERT INTO organizations (organization_id, name, admin_user_id, "
            "seats_purchased) VALUES (?, ?, ?, 5)",
            org_id, self.label("org"), user_id)
        # credits_granted is NOT NULL with no DEFAULT (023) -- it must be supplied. It is the
        # seat's original allocation; credits_remaining is what is left of it, so a freshly
        # seeded member has spent nothing and the two are equal. status and joined_at DO carry
        # DEFAULTs and are left to the schema. There is no `role` column on this table.
        cur.execute(
            "INSERT INTO organization_members (organization_id, user_id, "
            "credits_granted, credits_remaining) VALUES (?, ?, ?, ?)",
            org_id, user_id, credits, credits)
        return org_id

    def remove_org_membership(self, conn, org_id, user_id):
        """REACHABLE: removing a member is an ordinary product operation, and
        organization_members has NO FK to users, so the row simply goes away while the user
        and their jobs remain. THIS is the real missing-refund-target state."""
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM organization_members WHERE organization_id = ? AND user_id = ?",
            org_id, user_id)
        return cur.rowcount

    def orphan_free_training(self, conn, user_id):
        """Delete a user who has ONLY a free training.

        REACHABLE: lora_trainings.user_id has no FK, and with no jobs and no ledger rows
        nothing blocks the delete. Manual account removal reaches exactly this state.
        """
        cur = conn.cursor()
        cur.execute("DELETE FROM users WHERE user_id = ?", user_id)
        return cur.rowcount

    def orphan_paid_training_DRIFT(self, conn, user_id):
        """DRIFT FIXTURE -- explicitly NOT reachable in production.

        A PAID training has a retrain_charge ledger row, and FK_credit_tx_user blocks deleting
        its user. To reach the state at all the ledger row must be removed first, which is
        itself the corruption being modelled (the ledger is append-only by convention, not by
        constraint). NO constraint is disabled, dropped or bypassed: the delete succeeds only
        because the referencing row is genuinely gone.

        This exists because the ORPHAN_USER paid path must still be proven to behave, and
        because its CRITICAL log says exactly this: missing ledger history or manual deletion.
        """
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM credit_transactions WHERE user_id = ? AND transaction_type = ?",
            user_id, "retrain_charge")
        removed = cur.rowcount
        cur.execute("DELETE FROM users WHERE user_id = ?", user_id)
        return removed, cur.rowcount

    # -- reads -----------------------------------------------------------
    def balances(self, conn, user_id):
        cur = conn.cursor()
        cur.execute(
            "SELECT credits_remaining, monthly_credits_remaining, "
            "one_time_credits_remaining FROM users WHERE user_id = ?", user_id)
        row = cur.fetchone()
        return None if row is None else (int(row[0] or 0), int(row[1] or 0),
                                         int(row[2] or 0))

    def ledger(self, conn, job_id=None, user_id=None, kind=None):
        cur = conn.cursor()
        sql = ("SELECT transaction_id, user_id, amount, transaction_type, job_id "
               "FROM credit_transactions WHERE 1 = 1")
        args = []
        if job_id is not None:
            sql += " AND job_id = ?"
            args.append(job_id)
        if user_id is not None:
            sql += " AND user_id = ?"
            args.append(user_id)
        if kind is not None:
            sql += " AND transaction_type = ?"
            args.append(kind)
        cur.execute(sql, *args)
        return [tuple(r) for r in cur.fetchall()]

    def outbox_rows(self, conn):
        cur = conn.cursor()
        cur.execute("SELECT outbox_id, queue_name, payload FROM outbox ORDER BY outbox_id")
        return [tuple(r) for r in cur.fetchall()]

    def scalar(self, conn, sql, *args):
        cur = conn.cursor()
        cur.execute(sql, *args)
        row = cur.fetchone()
        return None if row is None else row[0]
