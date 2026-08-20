"""Org-aware credit resolution for Teams members.

WHY THIS EXISTS
A Teams employee is an ordinary users row — same login, same identity LoRA, same
generation path. What differs is WHICH credit pool their generations spend from.
After migration 016 a person can hold two balances at once:

    users.credits_remaining                  <- individual pool (existing product)
    organization_members.credits_remaining   <- Teams pool (their seat)

jobs.organization_id is the record of which pool a given job charged: NULL means
the individual pool, non-NULL means that org's pool. Because it is written at
reserve time, the refund path can read it back and return the credits to the
right place without re-deriving anything — which matters, since a person could
leave an org between submitting a job and that job failing.

RULE: org membership wins. If someone is an active member of an active org, their
generations spend org credits, and their personal balance is left alone.
"""


def get_active_membership(cursor, user_id):
    """Returns (organization_id, credits_remaining, membership_id) or None.

    Only ACTIVE members of ACTIVE orgs. A removed member or a suspended org falls
    back to the individual pool rather than being blocked outright — a suspended
    org shouldn't strand an employee who also has personal credits.

    Takes a cursor rather than opening its own connection so callers can run this
    inside their existing transaction (reserve_job_slot must, or the check and the
    decrement wouldn't be atomic).
    """
    cursor.execute("""
        SELECT m.organization_id, m.credits_remaining, m.membership_id
        FROM organization_members m
        JOIN organizations o ON o.organization_id = m.organization_id
        WHERE m.user_id = ? AND m.status = 'active' AND o.status = 'active'
    """, user_id)
    row = cursor.fetchone()
    if not row:
        return None
    return row[0], int(row[1] or 0), row[2]


def effective_credits(cursor, user_id, personal_credits):
    """The balance to show and to budget an image count against.

    Returns (credits, organization_id). organization_id is None for individual users.
    """
    membership = get_active_membership(cursor, user_id)
    if membership:
        org_id, org_credits, _ = membership
        return org_credits, org_id
    return personal_credits, None
