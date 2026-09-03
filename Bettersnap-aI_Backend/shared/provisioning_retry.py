"""Bounded re-dispatch for ACA PRE-CONTAINER replica-provisioning failures.

SCOPE
-----
This module is the runtime half of migrations 033/034. exec_reconcile decides WHAT happened;
this decides what to DO about the one class Azure can fix by trying again
(CLASS_PRE_CONTAINER_PROVISIONING -> ACTION_RETRY). Nothing else here ever retries.

EVERY function is CURSOR-LEVEL and PURE of connection management: the caller owns the
connection, the transaction and the single commit. That is deliberate. Requirement: a retry
must transition the row AND insert its outbox message in ONE transaction, and an exhaustion
must terminalize AND refund in ONE transaction. Splitting either across two commits opens a
crash window that either loses a job or double-refunds it. Passing `cur` in is what makes the
atomicity the caller's to guarantee, and what makes all of this testable with a fake cursor.

ATTEMPT SEMANTICS — ONE MEANING, NO OFF-BY-ONE
----------------------------------------------
    provisioning_attempts == number of DISTINCT ACA executions of this row that the
                             provisioning-retry path has handled.

MAX_PROVISIONING_EXECUTIONS is therefore the maximum TOTAL number of ACA executions a single
row may ever have, not a count of "extra tries":

    exec #1 fails pre-container -> attempts 0 -> 1, 1 < 3 -> RETRY   (exec #2 starts)
    exec #2 fails pre-container -> attempts 1 -> 2, 2 < 3 -> RETRY   (exec #3 starts)
    exec #3 fails pre-container -> attempts 2 -> 3, 3 == 3 -> EXHAUSTED (refund)

Total A100 executions with the default of 3: exactly 3. Retries granted: 2. After exhaustion
provisioning_attempts == MAX_PROVISIONING_EXECUTIONS exactly. The counter is incremented in
the SAME statement that performs the transition, and only for an execution id not already in
the history, so it can never run ahead of reality.

INVARIANT: provisioning_attempts == len(provisioning_execution_ids), always. The history is
the evidence; the counter is a denormalisation of it. `plan_attempt` derives the decision from
the HISTORY, never from the bare counter, so a drifted counter cannot grant a free retry.

IDEMPOTENCY
-----------
`provisioning_execution_ids` is a JSON array of the ACA execution ids already handled. A second
reconcile of the SAME execution id — duplicate timer tick, concurrent reaper, redelivered
outbox message — finds it present and does nothing: no increment, no transition, no outbox row.
Combined with the caller's row lock this is what makes at-least-once reconciliation safe.

FAIL-CLOSED HISTORY
-------------------
A malformed history value is NEVER silently reset. Resetting would hand the row a fresh budget
and could re-dispatch an execution that already burned three A100 starts. `parse_history`
raises HistoryCorrupt, the caller refuses to retry, and the row takes the ordinary
terminal/refund path instead.
"""
import json
import os
import uuid

# Maximum TOTAL ACA executions for one row, including the initial one. See ATTEMPT SEMANTICS.
MAX_PROVISIONING_EXECUTIONS = max(
    1, int(os.environ.get("MAX_PROVISIONING_EXECUTIONS", "3")))

# plan_attempt outcomes
PLAN_ALREADY_HANDLED = "already_handled"   # this exact execution was reconciled before
PLAN_RETRY = "retry"                       # budget remains; re-dispatch
PLAN_EXHAUSTED = "exhausted"               # budget spent; terminalize + refund

# Reasons a fused link cannot be used. Each is a FAIL-CLOSED stop, never a fallback to
# re-selecting a job by user_id/status.
LINK_OK = "ok"
LINK_MISSING = "missing"           # historical row, or never a fused run
LINK_NOT_FOUND = "not_found"       # linked job row is gone
LINK_WRONG_USER = "wrong_user"     # linked job belongs to someone else
LINK_TERMINAL = "terminal"         # linked job already completed/failed
LINK_UNEXPECTED_STATE = "unexpected_state"

# Job states a fused retry may return to waiting_lora.
FUSED_RECLAIMABLE_STATES = ("processing",)
_TERMINAL_JOB_STATES = ("completed", "failed")

# Outcomes of a guarded execution-id write.
RECORD_OK = "recorded"          # this call wrote the id
RECORD_STALE = "stale"          # the row moved on; the caller's execution is an ORPHAN
RECORD_MISSING = "missing"      # no such row

# How long a row may sit pointing at an ALREADY-HANDLED execution before we stop waiting and
# terminalize it as unclassified. Measured from the PERSISTED first_terminal_observed_at, so
# it survives restarts and cannot be reset by re-reading. Deliberately the same default as
# exec_reconcile.PROVISIONING_MAX_OBSERVATION: it is the same question ("how long do we keep
# looking before admitting we cannot attribute this?").
ORPHAN_OBSERVATION_S = int(os.environ.get("PROVISIONING_MAX_OBSERVATION", "1800"))

# terminalize_and_refund refund states
REFUND_DONE = "refunded"        # credits moved AND a ledger row was written
REFUND_NONE = "none"            # nothing owed (the row was already terminal)
REFUND_PENDING = "pending"      # credits are OWED but the target row does not exist

# Terminal class for a row whose provisioning history cannot be read. NOT an infra class: an
# unreadable history tells us nothing about why the run failed, so claiming infra would be a
# fabricated attribution.
CLASS_PROVISIONING_HISTORY_CORRUPT = "provisioning_history_corrupt"

# Operator-repair window for a corrupt history, measured from a DURABLE existing timestamp
# (COALESCE(dispatched_at, created_at)) because a row with no execution id has no per-attempt
# clock to stamp. Finite by construction: nothing about a corrupt history fixes itself, so the
# only question is how long an operator gets to repair it before the customer is made whole.
HISTORY_CORRUPT_OBSERVATION_S = int(
    os.environ.get("PROVISIONING_HISTORY_CORRUPT_CEILING", "1800"))

# plan_corrupt_history outcomes
CORRUPT_OBSERVE = "observe"
CORRUPT_TERMINAL = "terminal"

# plan_orphan outcomes
ORPHAN_RECOVER = "recover"      # a newer, unhandled execution exists — adopt it
ORPHAN_OBSERVE = "observe"      # inside the ceiling; keep looking
ORPHAN_TERMINAL = "terminal"    # ceiling expired; terminalize + refund once, unclassified


def same_user_id(left, right):
    """Are these two values the SAME SQL Server UNIQUEIDENTIFIER?

    SQL Server renders `uniqueidentifier` as an UPPERCASE string through pyodbc
    ('448C5F40-56F7-...'), while every caller-side id in this system is lowercase: Entra oids
    arrive lowercase, `uuid.uuid4()` produces lowercase, and queue payloads carry
    `str(user_id)` unchanged. A case-SENSITIVE `str(a) == str(b)` therefore compares a value
    read from the database against the same value carried through the queue and concludes they
    are different people. The integration suite caught exactly that in verify_fused_link: the
    second allocator reported `wrong_user` for its own user.

    The asymmetry is what makes it a trap. `WHERE user_id = ?` is case-INSENSITIVE, because
    SQL Server compares uniqueidentifier as a binary type -- so the SELECT finds the row and
    only the Python-side check disagrees.

    Parsed, not lowercased. `.lower()` would silently "work" for arbitrary identifiers that are
    not GUIDs at all and hide a genuine mismatch; uuid.UUID() accepts only real UUIDs in any
    canonical form and rejects everything else. None, empty, malformed and non-UUID values
    FAIL CLOSED as unequal -- an unparseable owner is never treated as a match.

    USE ONLY where both operands are UNIQUEIDENTIFIER user ids. ACA execution names are opaque
    identifiers, not GUIDs, and must keep comparing exactly.
    """
    try:
        return uuid.UUID(str(left)) == uuid.UUID(str(right))
    except (AttributeError, TypeError, ValueError):
        return False


class HistoryCorrupt(ValueError):
    """provisioning_execution_ids held something that is not a JSON list of strings."""


class RefundTargetMissing(RuntimeError):
    """The balance a refund was owed to did not exist, so no credits moved.

    Raised so the caller ROLLS BACK the whole terminal transaction. Committing the status
    change without the balance restore — or, worse, writing a refund ledger row for money that
    never moved — would leave the ledger asserting something the balances contradict. A
    rolled-back job stays non-terminal and is retried by the next reaper pass, which is the
    strictly safer failure mode.
    """


# ── history helpers ───────────────────────────────────────────────────────────
def parse_history(raw):
    """Parse provisioning_execution_ids. NULL/empty is an empty history (a row that has never
    been retried); anything else that is not a JSON list of non-empty strings is CORRUPT and
    raises. Never returns [] for malformed input — that would silently restore the budget."""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)
        if not text.strip():
            return []
        try:
            items = json.loads(text)
        except (TypeError, ValueError) as e:
            raise HistoryCorrupt("provisioning_execution_ids is not valid JSON: %s" % e)
    if not isinstance(items, list):
        raise HistoryCorrupt(
            "provisioning_execution_ids must be a JSON list, got %s" % type(items).__name__)
    out = []
    for item in items:
        if not isinstance(item, str) or not item.strip():
            raise HistoryCorrupt(
                "provisioning_execution_ids contains a non-string or empty entry")
        out.append(item)
    if len(set(out)) != len(out):
        raise HistoryCorrupt("provisioning_execution_ids contains duplicate execution ids")
    return out


def dump_history(history):
    return json.dumps(list(history))


def plan_attempt(raw_history, execution_id, attempts=None,
                 max_executions=None):
    """Decide what this terminal execution may do, from the HISTORY (not the bare counter).

    Returns (plan, new_history, new_attempts). For PLAN_ALREADY_HANDLED the history and count
    are returned unchanged so the caller writes nothing at all.

    `attempts` is the persisted counter, accepted only to detect drift: it must equal
    len(history). A mismatch is corruption, not something to paper over — it would mean either
    a lost increment (free extra A100 starts) or a phantom one.
    """
    max_executions = max_executions or MAX_PROVISIONING_EXECUTIONS
    history = parse_history(raw_history)
    if attempts is not None and int(attempts) != len(history):
        raise HistoryCorrupt(
            "provisioning_attempts=%s disagrees with %d recorded execution id(s)"
            % (attempts, len(history)))
    if not execution_id:
        raise HistoryCorrupt("cannot plan a provisioning attempt without an execution id")
    execution_id = str(execution_id)
    if execution_id in history:
        return PLAN_ALREADY_HANDLED, history, len(history)
    new_history = history + [execution_id]
    plan = PLAN_RETRY if len(new_history) < max_executions else PLAN_EXHAUSTED
    return plan, new_history, len(new_history)


# ── first terminal observation (per ATTEMPT) ──────────────────────────────────
_STAMP_SQL = {
    "jobs": (
        "UPDATE jobs SET first_terminal_observed_at = GETUTCDATE() "
        "WHERE job_id = ? AND external_execution_id = ? "
        "AND first_terminal_observed_at IS NULL "
        "AND status NOT IN ('completed', 'failed')"),
    "lora_trainings": (
        "UPDATE lora_trainings SET first_terminal_observed_at = GETUTCDATE() "
        "WHERE training_id = ? AND external_execution_id = ? "
        "AND first_terminal_observed_at IS NULL "
        "AND status NOT IN ('completed', 'failed')"),
}


def stamp_first_terminal(cur, table, key, execution_id):
    """Record WHEN this execution attempt was first seen terminal. Returns True if this call
    was the one that stamped it.

    Three guards, all required:
      * `external_execution_id = ?` — the row must still be on the execution we observed. If a
        retry already moved it on, the observation is stale and must not touch the new attempt.
      * `first_terminal_observed_at IS NULL` — never overwrite. The FIRST observation is what
        makes age monotonic; refreshing it would hold a row inside the ingestion grace forever.
      * not already terminal — nothing to observe on a finished row.

    Stamping is NOT a decision. It never retries, refunds or transitions; the next
    reconciliation pass re-reads telemetry and decides with an age that is now measurable.
    """
    if table not in _STAMP_SQL:
        raise ValueError("unknown table for first-terminal stamp: %r" % (table,))
    if not execution_id:
        return False
    cur.execute(_STAMP_SQL[table], key, str(execution_id))
    return cur.rowcount == 1


# ── inference retry ───────────────────────────────────────────────────────────
# States a job may be re-dispatched FROM. These are the non-terminal states an execution can
# be sitting in when its ACA execution goes terminal.
RETRYABLE_JOB_STATES = ("processing", "dispatching")


def retry_job(cur, job_id, execution_id, *, outbox_add, queue_name,
              max_executions=None):
    """ONE transaction's worth of work for a retryable inference execution.

    The caller must have opened a transaction and must commit exactly once afterwards. On
    return, either everything below is staged or nothing is:

      1. lock + re-verify the row (status non-terminal AND still on THIS execution);
      2. plan the attempt from the persisted history (duplicate -> no-op);
      3. record the execution id + increment the counter + transition to 'queued' +
         clear external_execution_id + reset first_terminal_observed_at, in ONE statement
         guarded on the same status/execution predicates (rowcount == 1);
      4. insert the retry outbox row.

    The outbox insert is LAST and inside the same transaction, so there is no window in which
    the job is 'queued' with no message. The caller must never enqueue directly.

    Returns a dict: {"plan", "attempts", "outbox_id", "message"}. plan == PLAN_RETRY means a
    retry was staged; PLAN_ALREADY_HANDLED means this execution was reconciled before and
    NOTHING was written; PLAN_EXHAUSTED means the caller must terminalize instead.

    Credits are deliberately untouched. A retry is a continuation of the SAME paid job: the
    user's reservation stands, no refund is issued between attempts, and no second reservation
    is created. The row stays 'queued' so the customer keeps seeing an in-progress job.
    """
    cur.execute(
        "SELECT status, user_id, job_params, provisioning_attempts, "
        "provisioning_execution_ids FROM jobs WITH (UPDLOCK, HOLDLOCK) WHERE job_id = ? "
        "AND external_execution_id = ?",
        job_id, str(execution_id),
    )
    row = cur.fetchone()
    if row is None:
        # Either the job is gone or it has already moved off this execution (a concurrent
        # reconciler retried it first). Both mean: not ours to act on.
        return {"plan": PLAN_ALREADY_HANDLED, "attempts": None,
                "outbox_id": None, "message": None, "reason": "row/execution no longer current"}
    status, user_id, job_params, attempts, raw_history = row[0], row[1], row[2], row[3], row[4]
    if status not in RETRYABLE_JOB_STATES:
        return {"plan": PLAN_ALREADY_HANDLED, "attempts": attempts,
                "outbox_id": None, "message": None,
                "reason": "job status %r is not retryable" % (status,)}

    plan, history, new_attempts = plan_attempt(
        raw_history, execution_id, attempts=attempts, max_executions=max_executions)
    if plan != PLAN_RETRY:
        return {"plan": plan, "attempts": new_attempts, "outbox_id": None,
                "message": None, "history": history}

    cur.execute(
        # ONE statement: history + counter + state + execution clear + clock reset.
        # first_terminal_observed_at MUST reset here — migration 033's runtime invariant. The
        # new attempt has its own clock; carrying the old one would make the retry look like
        # it had already exhausted the observation window the instant it was dispatched.
        "UPDATE jobs SET status = 'queued', external_execution_id = NULL, "
        "first_terminal_observed_at = NULL, dispatched_at = NULL, "
        "provisioning_attempts = ?, provisioning_execution_ids = ? "
        "WHERE job_id = ? AND external_execution_id = ? AND status = ?",
        new_attempts, dump_history(history), job_id, str(execution_id), status,
    )
    if cur.rowcount != 1:
        # Lost the race to a concurrent worker. Signal "handled elsewhere" and write nothing
        # more; the caller commits an empty transaction.
        return {"plan": PLAN_ALREADY_HANDLED, "attempts": attempts, "outbox_id": None,
                "message": None, "reason": "transition rowcount != 1 (concurrent reconciler)"}

    message = {"job_id": str(job_id), "user_id": str(user_id), "job_params": job_params}
    outbox_id = outbox_add(cur, queue_name, message)
    return {"plan": PLAN_RETRY, "attempts": new_attempts, "outbox_id": outbox_id,
            "message": message, "history": history}


# ── terminal failure + refund, in ONE transaction ─────────────────────────────
def _balance_moved(cur):
    """Did the refund UPDATE actually move a balance?

    rowcount 0 means the target row does not exist (deleted user, member who left the org), so
    NO mutation occurred -- SQL Server changed nothing, there is nothing to roll back. The
    caller must then skip the ledger row and record the debt durably instead of either
    inventing a ledger entry or abandoning the job non-terminal.
    """
    return cur.rowcount == 1


# -- THE canonical refund plan ------------------------------------------------
#
# WHY ONE STRUCTURE
# A refund is not a single number. A monthly-funded job is charged against TWO spendable
# buckets (monthly_credits_remaining and one_time_credits_remaining) plus the aggregate
# credits_remaining, and reserve_job_slot guarantees
#     monthly_credit_cost + one_time_credit_cost == credit_cost
# for those jobs. Restoring only the aggregate leaves the user unable to SPEND credits the
# ledger says they have -- the balance reads correctly and every generation still fails.
#
# The immediate path and the delayed compensator derive and apply the SAME plan. There is
# exactly one place that computes deltas and exactly one place that writes them.
#
# WHY `funding` IS AN EXPLICIT FIELD
# The other six fields cannot tell a LEGACY aggregate-only refund from a BUCKETED one: both
# can carry monthly_delta == 0. Inferring it from "are the buckets zero?" would silently
# accept a bucketed plan that lost its bucket values -- exactly the class of corruption this
# validator exists to reject. So the shape is recorded explicitly, once, at derivation time,
# and every rule is checked against it.
FUNDING_LEGACY = "legacy"            # pre-bucket job: only credits_remaining was debited
FUNDING_BUCKETED = "bucketed"        # monthly and/or one-time buckets were debited
FUNDING_ORGANIZATION = "organization"  # an organization_members balance was debited

PLAN_FIELDS = ("total", "user_id", "target", "funding", "organization_id",
               "aggregate_delta", "monthly_delta", "one_time_delta")


class RefundPlanInvalid(ValueError):
    """A refund plan is missing, malformed, negative, or internally inconsistent.

    ALWAYS fail closed: leave the debt pending for an operator rather than move a balance we
    cannot justify. A wrong refund is not better than a late one."""


class RefundMarkerNotCleared(RuntimeError):
    """The pending-refund marker did not clear after a successful payment.

    The caller must ROLL BACK the whole compensation. Committing a paid refund whose marker
    survives would let the next compensator pay it again."""


def _reserve_amounts(cur, job_id, credit_ledger):
    """The credits actually DEBITED for this job, from its job_reserve ledger rows.

    reserve_job_slot writes exactly one SIGNED NEGATIVE row per job, in the same transaction
    as the debit (`credit_ledger.record(cur, user_id, -credit_cost, REASON_JOB_RESERVE, ...)`).
    The sign is the whole point: a negative amount is a charge, and only a charge can justify
    a refund.

    abs() was unsafe. A zero or POSITIVE job_reserve row means no debit occurred -- it is a
    grant, a correction, or corruption -- and abs() would have read +40 as "40 credits were
    taken", authorising a refund for money the customer never paid.

    Returns the expected reserved amounts as positives (-amount per row). Raises
    RefundPlanInvalid for any row that is not a real, strictly-negative integer.
    """
    cur.execute(
        "SELECT amount FROM credit_transactions WHERE job_id = ? AND transaction_type = ?",
        job_id, credit_ledger.REASON_JOB_RESERVE)
    out = []
    for row in cur.fetchall() or ():
        amount = row[0]
        # bool is a subclass of int, and float/str would coerce. Neither is a legitimate INT
        # column value, so both are contradictory evidence rather than something to convert.
        if isinstance(amount, bool) or not isinstance(amount, int):
            raise RefundPlanInvalid(
                "job %s has a job_reserve ledger row whose amount is %r, not an integer"
                % (job_id, amount))
        if amount >= 0:
            raise RefundPlanInvalid(
                "job %s has a job_reserve ledger row of %s; a reserve must be a strictly "
                "negative debit, so this is not evidence that anything was charged"
                % (job_id, amount))
        out.append(-amount)
    return out


def _check_reserve_evidence(cur, job_id, credit_ledger, total, funding):
    """Cross-check the derived total against the reserve ledger, per funding shape.

    THE COMPATIBILITY RULE, stated precisely:

      bucketed (source_type 'monthly' or 'one_time')  -> a valid reserve row is REQUIRED
      organization-funded                             -> a valid reserve row is REQUIRED
      legacy aggregate-only (source_type IS NULL and  -> a reserve row is OPTIONAL; when one
      organization_id IS NULL)                           exists it must still be valid

    WHY IT IS NARROW. An earlier version tolerated a missing reserve row for EVERY job, which
    would let a broken MODERN reservation pass audit on job_params alone. It also rested on a
    false premise -- that credit_transactions arrived in migration 024. It did not:
    000_baseline creates the table, and 024's own header records that it "already existed ...
    but was DORMANT - never written to (0 rows)". 024 formalised the ledger's role and added
    its two indexes; job_reserve WRITES entered the source later.

    The permitted shape is defined by SEMANTICS, not by migration order. `source_type` is
    nullable and reserve_job_slot still defaults it to None, so a NULL does NOT prove the row
    predates migration 016 -- do not claim that. What NULL source_type + NULL organization_id
    DOES mean is EXPLICIT LEGACY AGGREGATE-ONLY funding: no bucket was recorded as charged, so
    the refund restores credits_remaining and nothing else.

    That is why the exemption is safe rather than merely convenient. A legacy plan has no
    monthly_delta and no one_time_delta to get wrong; the worst a missing reserve row can do
    is restore an aggregate that job_params already states. A BUCKETED or ORGANIZATION plan is
    different -- it names specific spendable balances -- so it must be corroborated.

    An ambiguous historical BUCKETED or ORGANIZATION job with no reserve row is not guessed
    at: it raises, and the caller records it as unresolved for operator review.
    """
    reserved = _reserve_amounts(cur, job_id, credit_ledger)
    if len(reserved) > 1:
        raise RefundPlanInvalid(
            "job %s has %d job_reserve ledger rows (%s); the charge is ambiguous"
            % (job_id, len(reserved), reserved))
    if not reserved:
        if funding == FUNDING_LEGACY:
            return          # pre-source_type job: job_params is the only record it has
        raise RefundPlanInvalid(
            "job %s is %s-funded but has no job_reserve ledger row; there is no evidence it "
            "was ever charged" % (job_id, funding))
    if reserved[0] != total:
        raise RefundPlanInvalid(
            "job %s job_params credit_cost=%s disagrees with the job_reserve ledger debit of "
            "%s" % (job_id, total, reserved[0]))


def build_refund_plan(cur, job_id, *, credit_ledger, json_module=json):
    """Derive the canonical plan from the LOCKED job row + job_params, cross-checked against
    the reserve ledger. Returns None when the job row is gone; raises RefundPlanInvalid when
    the evidence is missing, malformed or contradictory.

    NO AMOUNT IS EVER GUESSED. The previous version turned unparseable job_params into a
    one-credit refund, which is not fail-closed: it silently under-refunds a 40-credit job and
    writes a ledger row asserting that was the whole debt.

    Evidence rules:
      * job_params.credit_cost is the authoritative allocation and must be a positive int;
      * job_params must carry a coherent bucket split for a bucketed job;
      * the job_reserve ledger must corroborate it, per funding shape -- see
        _check_reserve_evidence for the exact rule and why it is narrow.
    """
    cur.execute(
        "SELECT job_params, user_id, source_type, organization_id FROM jobs WHERE job_id = ?",
        job_id)
    r = cur.fetchone()
    if r is None:
        return None
    raw_params, user_id, source_type, org_id = r[0], r[1], r[2], r[3]
    if not user_id:
        raise RefundPlanInvalid("job %s has no user_id; a refund cannot be attributed" % job_id)
    if raw_params is None or (isinstance(raw_params, str) and not raw_params.strip()):
        raise RefundPlanInvalid("job %s has no job_params; the amount is unknown" % job_id)
    try:
        params = json_module.loads(raw_params)
    except (TypeError, ValueError) as e:
        raise RefundPlanInvalid("job %s has unparseable job_params: %s" % (job_id, e))
    if not isinstance(params, dict):
        raise RefundPlanInvalid("job %s job_params is not an object" % job_id)
    total = _positive_int(params.get("credit_cost"), "credit_cost", job_id)

    if org_id:
        plan = {"total": total, "user_id": str(user_id), "target": TARGET_ORG,
                "funding": FUNDING_ORGANIZATION, "organization_id": str(org_id),
                "aggregate_delta": total, "monthly_delta": 0, "one_time_delta": 0}
    elif source_type == "monthly":
        monthly = _positive_int(params.get("monthly_credit_cost"), "monthly_credit_cost",
                                job_id, allow_zero=True)
        one_time = _positive_int(params.get("one_time_credit_cost"), "one_time_credit_cost",
                                 job_id, allow_zero=True)
        plan = {"total": total, "user_id": str(user_id), "target": TARGET_USER,
                "funding": FUNDING_BUCKETED, "organization_id": None,
                "aggregate_delta": total, "monthly_delta": monthly,
                "one_time_delta": one_time}
    elif source_type == "one_time":
        plan = {"total": total, "user_id": str(user_id), "target": TARGET_USER,
                "funding": FUNDING_BUCKETED, "organization_id": None,
                "aggregate_delta": total, "monthly_delta": 0, "one_time_delta": total}
    else:
        plan = {"total": total, "user_id": str(user_id), "target": TARGET_USER,
                "funding": FUNDING_LEGACY, "organization_id": None,
                "aggregate_delta": total, "monthly_delta": 0, "one_time_delta": 0}
    # The reserve cross-check needs the funding shape, so it runs once that is decided.
    _check_reserve_evidence(cur, job_id, credit_ledger, total, plan["funding"])
    # Validate at the point of derivation too, so a job whose stored allocation is internally
    # inconsistent (a split that does not sum to the charge) never becomes a live plan.
    return validate_refund_plan(plan)


def _positive_int(value, field, job_id, allow_zero=False):
    if isinstance(value, bool) or not isinstance(value, int):
        raise RefundPlanInvalid(
            "job %s %s is %r, not an integer; the amount will not be guessed"
            % (job_id, field, value))
    if value < 0 or (value == 0 and not allow_zero):
        raise RefundPlanInvalid("job %s %s=%s is out of range" % (job_id, field, value))
    return value


def validate_refund_plan(plan):
    """Strict, fail-closed validation. Every rule below exists because breaking it makes the
    balances and the ledger disagree.

    UNIVERSAL: aggregate_delta == total. The ledger records `total`, and credits_remaining is
    moved by aggregate_delta, so any difference is a guaranteed divergence.

    BY FUNDING SHAPE:
      legacy       -> no buckets at all
      bucketed     -> monthly_delta + one_time_delta == total (so a one-time-only refund must
                      carry one_time_delta == total, and a partial bucket is rejected)
      organization -> no personal buckets; the whole amount returns to the member row
    """
    if not isinstance(plan, dict):
        raise RefundPlanInvalid("refund plan is not an object")
    missing = [f for f in PLAN_FIELDS if f not in plan]
    if missing:
        raise RefundPlanInvalid("refund plan is missing %s" % ", ".join(missing))
    ints = {}
    for field in ("total", "aggregate_delta", "monthly_delta", "one_time_delta"):
        value = plan[field]
        if isinstance(value, bool) or not isinstance(value, int):
            raise RefundPlanInvalid("%s must be an int, got %r" % (field, value))
        if value < 0:
            raise RefundPlanInvalid("%s must not be negative (%s)" % (field, value))
        ints[field] = value
    if ints["total"] <= 0:
        raise RefundPlanInvalid("total must be positive")
    if not plan.get("user_id"):
        raise RefundPlanInvalid("refund plan has no user_id")
    if plan["target"] not in (TARGET_USER, TARGET_ORG):
        raise RefundPlanInvalid("unknown refund target %r" % (plan["target"],))
    if plan["funding"] not in (FUNDING_LEGACY, FUNDING_BUCKETED, FUNDING_ORGANIZATION):
        raise RefundPlanInvalid("unknown funding shape %r" % (plan["funding"],))

    # UNIVERSAL: the aggregate that moves must be the amount that is ledgered.
    if ints["aggregate_delta"] != ints["total"]:
        raise RefundPlanInvalid(
            "aggregate_delta (%s) must equal total (%s); balances and ledger would disagree"
            % (ints["aggregate_delta"], ints["total"]))

    funding = plan["funding"]
    if funding == FUNDING_ORGANIZATION:
        if plan["target"] != TARGET_ORG:
            raise RefundPlanInvalid("organization funding must use the organization target")
        if not plan.get("organization_id"):
            raise RefundPlanInvalid("organization funding with no organization_id")
        if ints["monthly_delta"] or ints["one_time_delta"]:
            raise RefundPlanInvalid("organization refunds never touch the personal buckets")
        return plan

    if plan["target"] != TARGET_USER:
        raise RefundPlanInvalid("%s funding must use the personal target" % funding)
    if plan.get("organization_id"):
        raise RefundPlanInvalid("personal target must not carry an organization_id")
    if funding == FUNDING_LEGACY:
        if ints["monthly_delta"] or ints["one_time_delta"]:
            raise RefundPlanInvalid(
                "legacy funding has no buckets, but monthly=%s one_time=%s"
                % (ints["monthly_delta"], ints["one_time_delta"]))
        return plan
    # bucketed
    if ints["monthly_delta"] + ints["one_time_delta"] != ints["total"]:
        raise RefundPlanInvalid(
            "bucketed refund: monthly + one_time (%s + %s) must equal total (%s)"
            % (ints["monthly_delta"], ints["one_time_delta"], ints["total"]))
    return plan


def apply_refund_plan(cur, plan, job_id=None):
    """THE ONLY place a refund balance is written. Returns True when a row actually moved.

    Both the immediate terminalize-and-refund and the delayed compensator call this, so a
    delayed monthly or mixed refund restores exactly the same buckets as an immediate one.
    """
    user_id = plan["user_id"]
    if plan["funding"] == FUNDING_ORGANIZATION:
        cur.execute(
            "UPDATE organization_members SET credits_remaining = credits_remaining + ? "
            "WHERE user_id = ? AND organization_id = ?",
            plan["aggregate_delta"], user_id, plan["organization_id"])
        return _balance_moved(cur)
    if plan["funding"] == FUNDING_BUCKETED:
        # A plan transition changes the persistent bucket's UNIT (images on one-time,
        # credits on monthly). Never apply an old-unit refund to a new-unit account: keep
        # the existing durable refund marker for Support review instead of creating a 5x
        # over/under-credit. Normal immediate refunds still take this path unchanged.
        if job_id is not None:
            cur.execute("SELECT source_type FROM jobs WHERE job_id = ?", job_id)
            source_row = cur.fetchone()
            cur.execute("SELECT subscription_type FROM users WHERE user_id = ?", user_id)
            user_row = cur.fetchone()
            if not source_row or not user_row or source_row[0] != user_row[0]:
                return False
        cur.execute(
            "UPDATE users SET "
            "monthly_credits_remaining = monthly_credits_remaining + ?, "
            "one_time_credits_remaining = one_time_credits_remaining + ?, "
            "credits_remaining = credits_remaining + ? "
            "WHERE user_id = ?",
            plan["monthly_delta"], plan["one_time_delta"], plan["aggregate_delta"], user_id)
        return _balance_moved(cur)
    cur.execute(
        "UPDATE users SET credits_remaining = credits_remaining + ? WHERE user_id = ?",
        plan["aggregate_delta"], user_id)
    return _balance_moved(cur)


def terminalize_and_refund(cur, job_id, *, credit_ledger, json_module=json):
    """Fail a job and refund its credit, EXACTLY ONCE, on the caller's cursor.

    The exactly-once guarantee is the state transition itself: `status NOT IN
    ('failed','completed')` plus the rowcount check. Everything downstream is conditional on
    that rowcount, so a duplicate call refunds nothing.

    Returns (transitioned, refund_amount, refund_state). REFUND_PENDING means the credits are
    OWED -- either the target row does not exist, or the evidence for the amount is not good
    enough to act on. In BOTH cases nothing is moved and nothing is ledgered; the caller
    records the obligation durably.
    """
    cur.execute(
        "UPDATE jobs SET status = 'failed', completed_at = GETUTCDATE() "
        "WHERE job_id = ? AND status NOT IN ('failed', 'completed')",
        job_id,
    )
    if cur.rowcount != 1:
        return False, 0, REFUND_NONE
    try:
        plan = build_refund_plan(cur, job_id, credit_ledger=credit_ledger,
                                 json_module=json_module)
    except RefundPlanInvalid:
        # Insufficient or contradictory evidence. The transition stands so the job is not
        # stuck, but NO amount is guessed: the caller records an unresolved obligation for
        # operator review.
        return True, 0, REFUND_PENDING
    if plan is None:
        return True, 0, REFUND_PENDING
    moved = apply_refund_plan(cur, plan, job_id)
    # LEDGER ONLY AFTER THE MONEY MOVED, so a ledger row always describes a real mutation.
    if not moved:
        return True, plan["total"], REFUND_PENDING
    credit_ledger.record(cur, plan["user_id"], plan["total"],
                         credit_ledger.REASON_JOB_REFUND, job_id)
    return True, plan["total"], REFUND_DONE


def exhaust_job(cur, job_id, execution_id, *, credit_ledger, max_executions=None):
    """Budget spent on an inference job: record the final execution id, terminalize and refund,
    all on ONE cursor so the caller commits once. No outbox row is ever written here.

    Returns (transitioned, refund, attempts, refund_state)."""
    cur.execute(
        "SELECT status, provisioning_attempts, provisioning_execution_ids "
        "FROM jobs WITH (UPDLOCK, HOLDLOCK) WHERE job_id = ? AND external_execution_id = ?",
        job_id, str(execution_id),
    )
    row = cur.fetchone()
    if row is None:
        return False, 0, None, REFUND_NONE
    status, attempts, raw_history = row[0], row[1], row[2]
    if status in _TERMINAL_JOB_STATES:
        return False, 0, attempts, REFUND_NONE
    plan, history, new_attempts = plan_attempt(
        raw_history, execution_id, attempts=attempts, max_executions=max_executions)
    if plan != PLAN_ALREADY_HANDLED:
        cur.execute(
            "UPDATE jobs SET provisioning_attempts = ?, provisioning_execution_ids = ? "
            "WHERE job_id = ? AND external_execution_id = ?",
            new_attempts, dump_history(history), job_id, str(execution_id),
        )
        if cur.rowcount != 1:
            return False, 0, attempts, REFUND_NONE
    transitioned, refund, state = terminalize_and_refund(
        cur, job_id, credit_ledger=credit_ledger)
    return transitioned, refund, new_attempts, state


# ── fused train_infer linkage ─────────────────────────────────────────────────
def allocate_fused_job(cur, training_id, user_id):
    """Bind this training run to EXACTLY ONE generation job, once, deterministically.

    Replaces the transient `SELECT TOP 1 ... ORDER BY created_at` local variable with a
    persisted link. Inside the caller's single transaction:

      * lock the training row and read fused_job_id;
      * if a link already exists, REUSE it — never re-select. A second dispatch (retry, queue
        redelivery) that re-ran the selection could pick a different job than the first attempt
        claimed, generating for the wrong job or for two;
      * otherwise pick one eligible waiting_lora job with ORDER BY created_at, job_id. The
        job_id tie-break is what makes the choice deterministic when two rows share a
        created_at value;
      * verify same user + expected state, then transition waiting_lora -> processing and
        persist the link, rowcount-checking BOTH.

    Returns (fused_job_id | None, status_str). None means plain MODE=train, which is the
    correct, safe outcome whenever no job is parked or the claim is lost.
    """
    cur.execute(
        "SELECT fused_job_id, user_id FROM lora_trainings WITH (UPDLOCK, HOLDLOCK) "
        "WHERE training_id = ?",
        training_id,
    )
    trow = cur.fetchone()
    if trow is None:
        return None, "training row missing"
    existing = trow[0]
    if existing:
        ok, reason = verify_fused_link(cur, existing, user_id,
                                        expected_states=("processing", "waiting_lora"))
        if not ok:
            # Fail CLOSED. A broken link is never a licence to go pick another job.
            return None, "existing link unusable: %s" % reason
        # Reclaim only that job, and only if it is parked again (e.g. after a retry un-claim).
        cur.execute(
            "UPDATE jobs SET status = 'processing', dispatched_at = GETUTCDATE() "
            "WHERE job_id = ? AND status = 'waiting_lora'",
            existing,
        )
        return str(existing), "reused existing link"

    cur.execute(
        # ORDER BY created_at, job_id — the tie-break is the whole point (defect 1 in 034).
        "SELECT TOP 1 job_id FROM jobs WHERE user_id = ? AND status = 'waiting_lora' "
        "ORDER BY created_at, job_id",
        user_id,
    )
    row = cur.fetchone()
    if not row:
        return None, "no parked job"
    candidate = str(row[0])
    cur.execute(
        "UPDATE jobs SET status = 'processing', dispatched_at = GETUTCDATE() "
        "WHERE job_id = ? AND status = 'waiting_lora' AND user_id = ?",
        candidate, user_id,
    )
    if cur.rowcount != 1:
        return None, "claim lost (concurrent)"
    cur.execute(
        # Guarded on fused_job_id IS NULL so two concurrent allocators cannot both bind. The
        # filtered unique index (034) is the second line of defence if they somehow do.
        "UPDATE lora_trainings SET fused_job_id = ? "
        "WHERE training_id = ? AND fused_job_id IS NULL",
        candidate, training_id,
    )
    if cur.rowcount != 1:
        # Another allocator won. Raise so the caller rolls back the job claim too — committing
        # a 'processing' job with no link would strand it until the reaper timed it out.
        raise FusedLinkConflict(
            "training %s was bound concurrently; not binding %s" % (training_id, candidate))
    return candidate, "allocated"


class FusedLinkConflict(RuntimeError):
    """Two allocators raced for the same training row. The loser must roll back."""


def verify_fused_link(cur, fused_job_id, user_id, expected_states):
    """Is this persisted link usable? Returns (ok, reason). Every failure is terminal for the
    fused path — the caller must NOT substitute another job."""
    if not fused_job_id:
        return False, LINK_MISSING
    cur.execute("SELECT user_id, status FROM jobs WHERE job_id = ?", fused_job_id)
    jrow = cur.fetchone()
    if jrow is None:
        return False, LINK_NOT_FOUND
    # UNIQUEIDENTIFIER: SQL returns it UPPERCASE, the caller carries it lowercase.
    if not same_user_id(jrow[0], user_id):
        return False, LINK_WRONG_USER
    if jrow[1] in _TERMINAL_JOB_STATES:
        return False, LINK_TERMINAL
    if jrow[1] not in expected_states:
        return False, LINK_UNEXPECTED_STATE
    return True, LINK_OK


def retry_fused_training(cur, training_id, execution_id, *, outbox_add, queue_name,
                         max_executions=None):
    """ONE transaction: return the linked generation job to the parked pool and re-queue the
    training run. The caller commits once.

      * the fused job is found ONLY via the persisted fused_job_id — never re-selected;
      * a missing / terminal / wrong-user / unexpected-state link fails closed (no retry);
      * fused_job_id is RETAINED, so the next dispatch reclaims the same job;
      * the training row goes to 'queued' (the dispatcher's retryable state), loses its
        execution id, and has its per-attempt clock reset;
      * the attempt is recorded exactly once;
      * the redispatch message goes through the outbox, in this transaction. There is no
        direct queue send anywhere on this path.
    """
    cur.execute(
        "SELECT status, user_id, fused_job_id, provisioning_attempts, "
        "provisioning_execution_ids FROM lora_trainings WITH (UPDLOCK, HOLDLOCK) "
        "WHERE training_id = ? AND external_execution_id = ?",
        training_id, str(execution_id),
    )
    row = cur.fetchone()
    if row is None:
        return {"plan": PLAN_ALREADY_HANDLED, "outbox_id": None, "message": None,
                "fused_job_id": None, "reason": "row/execution no longer current"}
    status, user_id, fused_job_id, attempts, raw_history = (
        row[0], row[1], row[2], row[3], row[4])
    if status in ("completed", "failed"):
        return {"plan": PLAN_ALREADY_HANDLED, "outbox_id": None, "message": None,
                "fused_job_id": fused_job_id, "reason": "training already terminal"}

    plan, history, new_attempts = plan_attempt(
        raw_history, execution_id, attempts=attempts, max_executions=max_executions)
    if plan != PLAN_RETRY:
        return {"plan": plan, "outbox_id": None, "message": None,
                "fused_job_id": fused_job_id, "history": history}

    if fused_job_id:
        ok, reason = verify_fused_link(cur, fused_job_id, user_id,
                                        expected_states=FUSED_RECLAIMABLE_STATES)
        if not ok:
            # FAIL CLOSED: do not retry a fused run whose generation job we cannot account
            # for, and never go looking for a replacement.
            return {"plan": PLAN_EXHAUSTED, "outbox_id": None, "message": None,
                    "fused_job_id": fused_job_id,
                    "reason": "fused link unusable: %s" % reason, "link_error": reason}
        cur.execute(
            "UPDATE jobs SET status = 'waiting_lora', dispatched_at = NULL "
            "WHERE job_id = ? AND status = 'processing' AND user_id = ?",
            fused_job_id, user_id,
        )
        if cur.rowcount != 1:
            return {"plan": PLAN_EXHAUSTED, "outbox_id": None, "message": None,
                    "fused_job_id": fused_job_id,
                    "reason": "could not park the linked job", "link_error": LINK_UNEXPECTED_STATE}

    cur.execute(
        # fused_job_id is deliberately NOT cleared: the next dispatch must reclaim this exact
        # job. Clearing it would let allocate_fused_job pick a different one.
        "UPDATE lora_trainings SET status = 'queued', external_execution_id = NULL, "
        "first_terminal_observed_at = NULL, "
        "provisioning_attempts = ?, provisioning_execution_ids = ? "
        "WHERE training_id = ? AND external_execution_id = ? AND status = ?",
        new_attempts, dump_history(history), training_id, str(execution_id), status,
    )
    if cur.rowcount != 1:
        return {"plan": PLAN_ALREADY_HANDLED, "outbox_id": None, "message": None,
                "fused_job_id": fused_job_id,
                "reason": "transition rowcount != 1 (concurrent reconciler)"}

    message = {"training_id": str(training_id), "user_id": str(user_id)}
    outbox_id = outbox_add(cur, queue_name, message)
    return {"plan": PLAN_RETRY, "attempts": new_attempts, "outbox_id": outbox_id,
            "message": message, "fused_job_id": fused_job_id, "history": history}


def record_training_attempt(cur, training_id, execution_id, max_executions=None):
    """Record the FINAL handled execution id on an exhausting training run, once.

    Returns (recorded, attempts). Used by the exhaustion path so the same execution can never
    be counted twice, and so the persisted history remains a complete audit trail of every ACA
    execution this run consumed. The caller then terminalizes in the SAME transaction.
    """
    cur.execute(
        "SELECT provisioning_attempts, provisioning_execution_ids "
        "FROM lora_trainings WITH (UPDLOCK, HOLDLOCK) "
        "WHERE training_id = ? AND external_execution_id = ?",
        training_id, str(execution_id),
    )
    row = cur.fetchone()
    if row is None:
        return False, None
    attempts, raw_history = row[0], row[1]
    plan, history, new_attempts = plan_attempt(
        raw_history, execution_id, attempts=attempts, max_executions=max_executions)
    if plan == PLAN_ALREADY_HANDLED:
        return False, new_attempts
    cur.execute(
        "UPDATE lora_trainings SET provisioning_attempts = ?, provisioning_execution_ids = ? "
        "WHERE training_id = ? AND external_execution_id = ?",
        new_attempts, dump_history(history), training_id, str(execution_id),
    )
    return cur.rowcount == 1, new_attempts

# -- guarded execution-id persistence -----------------------------------------
_RECORD_SQL = {
    "jobs": ("UPDATE jobs SET external_execution_id = ? "
             "WHERE job_id = ? AND status = ? AND external_execution_id IS NULL",
             "SELECT status, external_execution_id FROM jobs WHERE job_id = ?"),
    "lora_trainings": (
        "UPDATE lora_trainings SET status = 'training', external_execution_id = ? "
        "WHERE training_id = ? AND status = ? AND external_execution_id IS NULL",
        "SELECT status, external_execution_id FROM lora_trainings WHERE training_id = ?"),
}

# The state a dispatch is expected to be in when it reports its execution id, plus the one
# benign state the container itself may already have moved the row to. Both writes still
# require external_execution_id IS NULL, which is the clause that actually prevents a late
# worker from overwriting a NEWER attempt.
_RECORD_EXPECTED = {"jobs": ("dispatching", "processing"),
                    "lora_trainings": ("dispatching",)}


def record_execution_id(cur, table, key, execution_id):
    """Write an execution id ONLY onto the attempt that is still waiting for one.

    Returns (RECORD_OK | RECORD_STALE | RECORD_MISSING, current_execution_id).

    THE RACE THIS CLOSES. A dispatch can block inside begin_start for longer than the
    ownership lease (180s) and longer than REAPER_DISPATCHING_MINUTES (15 min). In that window
    the reaper can recover the row, reconcile it, retry it, and a NEW dispatch can start a new
    execution. When the original worker finally returns, a blind
    ``UPDATE jobs SET external_execution_id = ?`` would overwrite the new attempt's id with a
    spent one, pinning the row to an execution that is already in its history — from which no
    verdict is reachable.

    So the write is guarded on ``external_execution_id IS NULL``: it can fill an empty slot,
    never replace an occupied one. On RECORD_STALE the caller's own execution is an ORPHAN
    running with nothing pointing at it; the caller must LOG it and must NOT retry the write.
    """
    if table not in _RECORD_SQL:
        raise ValueError("unknown table for execution-id record: %r" % (table,))
    update_sql, read_sql = _RECORD_SQL[table]
    for expected in _RECORD_EXPECTED[table]:
        cur.execute(update_sql, str(execution_id), key, expected)
        if cur.rowcount == 1:
            return RECORD_OK, str(execution_id)
    cur.execute(read_sql, key)
    row = cur.fetchone()
    if row is None:
        return RECORD_MISSING, None
    return RECORD_STALE, row[1]


# -- bounded lifecycle for an ALREADY-HANDLED execution -----------------------
def plan_orphan(*, status, current_execution_id, history, candidate_execution_id,
                age, ceiling=None):
    """What to do with a row whose persisted execution was ALREADY reconciled.

    Reaching this state means the row points at an execution that has already had its verdict
    — normally because a crash-window recovery adopted a spent execution, or a late worker
    wrote a stale id. Left alone the row reconciles to "nothing to do" on every tick forever:
    the customer paid, no images exist, and no refund is ever issued. Before bounded retries
    this state was unreachable (one job had one execution), which is why nothing bounded it.

    Policy, in order:

      1. RECOVER -- a newer execution exists that is NOT in the history. The row is simply
         pointing at the wrong attempt; adopt the newer one and let the next tick classify it
         normally. Recovery is always preferred to refunding: the current attempt may well be
         healthy, and refunding it would be premature.
      2. OBSERVE -- no newer execution, but the durable clock has not run out. Keep looking;
         Log Analytics and the ACA executions list both lag.
      3. TERMINAL -- no newer execution and the ceiling has expired. Terminalize and refund
         ONCE as unclassified. We refund because the user got nothing; we do NOT claim infra,
         because we genuinely cannot attribute it.

    `age` is seconds since the PERSISTED first_terminal_observed_at, or None when unknown. An
    unknown clock can never terminalize -- it OBSERVES, and the caller stamps the clock so a
    later tick has a real age. This is pure: the caller does the discovery and the I/O.
    """
    ceiling = ORPHAN_OBSERVATION_S if ceiling is None else ceiling
    if status in _TERMINAL_JOB_STATES:
        return ORPHAN_OBSERVE, "row is already terminal"
    known = set(parse_history(history) if not isinstance(history, (list, tuple)) else history)
    if candidate_execution_id and str(candidate_execution_id) not in known:
        if str(candidate_execution_id) != str(current_execution_id):
            return ORPHAN_RECOVER, str(candidate_execution_id)
    if age is None:
        return ORPHAN_OBSERVE, "terminal timestamp unknown; cannot age the orphan"
    if age < ceiling:
        return ORPHAN_OBSERVE, ("orphaned execution observed %.0fs ago; within the %ss ceiling"
                                % (age, ceiling))
    return ORPHAN_TERMINAL, ("execution %s was already reconciled and no newer execution "
                             "exists after %ss" % (current_execution_id, ceiling))


def adopt_execution(cur, job_id, stale_execution_id, new_execution_id):
    """Point a row at the newer execution discovered by plan_orphan, and reset the per-attempt
    clock so the adopted attempt is aged from when WE started watching it rather than
    inheriting the orphan's age. Guarded on the stale id so two reapers cannot both adopt."""
    cur.execute(
        "UPDATE jobs SET external_execution_id = ?, first_terminal_observed_at = NULL "
        "WHERE job_id = ? AND external_execution_id = ? "
        "AND status NOT IN ('completed', 'failed')",
        str(new_execution_id), job_id, str(stale_execution_id),
    )
    return cur.rowcount == 1


def terminalize_orphan(cur, job_id, execution_id, *, credit_ledger):
    """Terminalize + refund an orphaned row, exactly once, on the caller's cursor.

    Guarded on the stale execution id so a row that has since been adopted or retried is not
    terminalized by a stale decision. The one-refund guarantee is terminalize_and_refund's own
    status/rowcount guard, so repeated reaper ticks refund at most once.
    """
    cur.execute(
        "SELECT status FROM jobs WITH (UPDLOCK, HOLDLOCK) "
        "WHERE job_id = ? AND external_execution_id = ?",
        job_id, str(execution_id),
    )
    row = cur.fetchone()
    if row is None or row[0] in _TERMINAL_JOB_STATES:
        return False, 0, REFUND_NONE
    return terminalize_and_refund(cur, job_id, credit_ledger=credit_ledger)

# -- durable pending-refund debt, and its exactly-once settlement -------------
#
# WHY THIS EXISTS AND WHY IT NEEDS NO MIGRATION
# A refund can be owed to a balance row that no longer exists (a deleted user, a member who
# left the organization). Three bad options were rejected:
#   * ledger it anyway  -> the ledger asserts money moved when none did;
#   * roll the whole transition back -> the paid job sits in processing/dispatching FOREVER;
#   * log CRITICAL and move on -> the debt is invisible and never settled.
# So the job DOES terminalize, and the debt is written down in a durable, queryable place.
#
# The store is jobs.job_params (NVARCHAR(MAX)) under the existing `_failure` stamp, and the
# exactly-once guard is the existing credit ledger: a settled refund always has exactly one
# credit_transactions row with (job_id, transaction_type='job_refund'), and that table already
# carries IX_credit_tx_job on job_id. Both already exist, so no column, status, table or
# migration is introduced.
# The marker stores the COMPLETE plan (see PLAN_FIELDS), never just a total: the compensator
# has to restore the same buckets the immediate path would have.
REFUND_PENDING_KEY = "refund_pending"
TARGET_USER = "user"
TARGET_ORG = "organization"


def _load_params(cur, job_id):
    cur.execute("SELECT job_params FROM jobs WHERE job_id = ?", job_id)
    row = cur.fetchone()
    if row is None:
        return None
    try:
        params = json.loads(row[0]) if row[0] else {}
    except (TypeError, ValueError):
        params = {}
    return params if isinstance(params, dict) else {}


def mark_refund_pending(cur, job_id, plan):
    """Record, durably, the COMPLETE refund plan that could not be paid.

    Persisting only a total was a real accounting defect: the compensator could not know which
    buckets to restore, so a delayed monthly or mixed refund credited the aggregate and left
    the spendable buckets short.

    Returns True only if the marker was durably written. The caller MUST NOT commit a terminal
    state when this returns False -- doing so would lose the refund obligation entirely.
    """
    validate_refund_plan(plan)
    return _write_pending(cur, job_id, {k: plan[k] for k in PLAN_FIELDS})


def mark_refund_unresolved(cur, job_id, reason):
    """Record that a refund is OWED but its amount could not be established.

    This is what "insufficient evidence" looks like on disk. No amount is invented, so the
    compensator can never act on it automatically -- it stays visible until a human resolves
    it. The job still terminalizes, so it is not stuck in processing/dispatching forever.
    """
    return _write_pending(cur, job_id, {"unresolved": True, "reason": str(reason)[:400]})


def _write_pending(cur, job_id, payload):
    params = _load_params(cur, job_id)
    if params is None:
        return False
    failure = params.get("_failure")
    if not isinstance(failure, dict):
        failure = {}
    failure[REFUND_PENDING_KEY] = payload
    params["_failure"] = failure
    cur.execute("UPDATE jobs SET job_params = ? WHERE job_id = ?",
                json.dumps(params), job_id)
    return cur.rowcount == 1


def clear_refund_pending(cur, job_id):
    """Remove the debt marker once it has been settled. Idempotent."""
    params = _load_params(cur, job_id)
    if params is None:
        return False
    failure = params.get("_failure")
    if not isinstance(failure, dict) or REFUND_PENDING_KEY not in failure:
        return False
    failure.pop(REFUND_PENDING_KEY, None)
    params["_failure"] = failure
    cur.execute("UPDATE jobs SET job_params = ? WHERE job_id = ?",
                json.dumps(params), job_id)
    return cur.rowcount == 1


def read_refund_pending(cur, job_id):
    """The pending-refund debt on this job, or None."""
    params = _load_params(cur, job_id)
    if not params:
        return None
    failure = params.get("_failure")
    if not isinstance(failure, dict):
        return None
    pending = failure.get(REFUND_PENDING_KEY)
    return pending if isinstance(pending, dict) else None


# already_refunded verdicts
REFUND_ROWS_NONE = "none"          # nothing ledgered -> eligible to compensate
REFUND_ROWS_SETTLED = "settled"    # exactly one row, right user, right amount
REFUND_ROWS_CONFLICT = "conflict"  # anything else -> operator review


def already_refunded(cur, job_id, credit_ledger, plan=None):
    """Classify the job_refund ledger rows for this job. Returns (verdict, rows).

    A refund is SETTLED only when there is EXACTLY ONE row, its user_id equals the plan's, and
    its amount equals the plan's total. Everything else -- zero rows aside -- is a CONFLICT
    that must stay pending for a human:

      * two rows mean the job was refunded twice, or once correctly and once wrongly;
      * a row for a different user means the credits went to the wrong place;
      * a row for a different amount means something other than this debt was settled.

    Using `any(...)` over the rows was wrong: one correct row alongside one incorrect row
    would have read as settled and cleared the marker, permanently hiding the bad row.

    UPDLOCK/HOLDLOCK so two concurrent compensators serialize on the same key range rather
    than both concluding "not yet refunded".
    """
    cur.execute(
        "SELECT transaction_id, user_id, amount FROM credit_transactions "
        "WITH (UPDLOCK, HOLDLOCK) WHERE job_id = ? AND transaction_type = ?",
        job_id, credit_ledger.REASON_JOB_REFUND,
    )
    rows = list(cur.fetchall() or ())
    if not rows:
        return REFUND_ROWS_NONE, rows
    if len(rows) > 1:
        return REFUND_ROWS_CONFLICT, rows
    # transaction_id is selected so the row is identifiable in an operator's follow-up query;
    # the verdict itself turns on user_id and amount.
    row_user, row_amount = rows[0][1], rows[0][2]
    if plan is None:
        return REFUND_ROWS_CONFLICT, rows
    try:
        amount_ok = int(row_amount) == int(plan["total"])
    except (TypeError, ValueError):
        amount_ok = False
    # Same UNIQUEIDENTIFIER normalisation: a refund ledger row read back from SQL is
    # uppercase, while the persisted plan carries the lowercase id it was built from.
    user_ok = same_user_id(row_user, plan["user_id"])
    if amount_ok and user_ok:
        return REFUND_ROWS_SETTLED, rows
    return REFUND_ROWS_CONFLICT, rows


def compensate_pending_refund(cur, job_id, *, credit_ledger):
    """Settle one recorded refund debt, at most once, on the caller's cursor.

    Returns REFUND_DONE / REFUND_PENDING / REFUND_NONE. The persisted plan is applied through
    the SAME apply_refund_plan the immediate path uses, so a delayed refund restores exactly
    the same buckets in exactly the same amounts.

    Everything that is not provably safe stays PENDING and untouched for operator review: an
    unresolved marker, a plan that fails validation, or conflicting ledger rows.

    Raises RefundMarkerNotCleared if the payment succeeded but the marker did not clear -- the
    caller MUST roll back, because a paid refund with a surviving marker would be paid again.
    """
    raw = read_refund_pending(cur, job_id)
    if not raw:
        return REFUND_NONE
    if raw.get("unresolved"):
        # No amount was ever established. Never guess one.
        return REFUND_PENDING
    try:
        plan = validate_refund_plan(raw)
    except RefundPlanInvalid:
        # FAIL CLOSED. Leave the marker exactly as found so an operator can see and repair it.
        return REFUND_PENDING
    verdict, _rows = already_refunded(cur, job_id, credit_ledger, plan=plan)
    if verdict == REFUND_ROWS_SETTLED:
        # A normal path settled it after the marker was written. Clear and stop.
        if not clear_refund_pending(cur, job_id):
            raise RefundMarkerNotCleared(
                "job %s was already settled but its marker did not clear" % job_id)
        return REFUND_NONE
    if verdict == REFUND_ROWS_CONFLICT:
        # Do not pay on top of rows we cannot account for, and do not clear the marker.
        return REFUND_PENDING
    if not apply_refund_plan(cur, plan, job_id):
        return REFUND_PENDING
    credit_ledger.record(cur, plan["user_id"], plan["total"],
                         credit_ledger.REASON_JOB_REFUND, job_id)
    if not clear_refund_pending(cur, job_id):
        # The money moved and the ledger row exists, but the debt is still recorded. Rolling
        # the WHOLE transaction back is the only safe outcome: committing here would leave a
        # live marker over a paid refund, and the next tick would pay it a second time.
        raise RefundMarkerNotCleared(
            "job %s refund applied but the pending marker did not clear" % job_id)
    return REFUND_DONE


# -- corrupt provisioning history: finite, fail-closed ------------------------
def plan_corrupt_history(age, ceiling=None):
    """What to do with a row whose provisioning_execution_ids cannot be parsed.

    A corrupt history means we cannot prove which ACA executions were already reconciled. So
    we must NOT adopt a "newer" execution (we cannot show it is unhandled) and must NOT retry
    (we cannot show budget remains). Skipping the row forever is equally unacceptable: the
    customer paid and the reaper would pass over it on every tick.

    So: observe inside a finite operator-repair window, then terminalize and refund exactly
    once. `age` is seconds since COALESCE(dispatched_at, created_at) -- a DURABLE existing
    timestamp, chosen because a row with no execution id has no per-attempt clock to stamp.
    An unknown age still terminalizes rather than waiting forever, because there is nothing
    left to wait for.
    """
    ceiling = HISTORY_CORRUPT_OBSERVATION_S if ceiling is None else ceiling
    if age is not None and age < ceiling:
        return CORRUPT_OBSERVE, (
            "provisioning history unreadable; observing for %.0fs more before terminalizing"
            % (ceiling - age))
    return CORRUPT_TERMINAL, (
        "provisioning history unreadable and unrepaired after %ss; refunding without "
        "attributing a cause" % ceiling)


def terminalize_corrupt_history(cur, job_id, *, credit_ledger):
    """Terminalize + refund a corrupt-history row, exactly once.

    The history is NEVER reset or reinterpreted -- it is left exactly as found so an operator
    can still inspect it. Only the job's lifecycle is closed.
    """
    cur.execute(
        "SELECT status FROM jobs WITH (UPDLOCK, HOLDLOCK) WHERE job_id = ?", job_id)
    row = cur.fetchone()
    if row is None or row[0] in _TERMINAL_JOB_STATES:
        return False, 0, REFUND_NONE
    return terminalize_and_refund(cur, job_id, credit_ledger=credit_ledger)
