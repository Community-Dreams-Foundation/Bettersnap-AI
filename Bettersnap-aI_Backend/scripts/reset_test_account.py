#!/usr/bin/env python3
"""Tear down a TEST account completely: SQL rows, then blobs.

WHY THIS EXISTS
Wiping a test user by hand means seven tables in foreign-key order plus three blob
containers, with the result images keyed by job_id — so the job ids have to be captured
BEFORE the rows are deleted or the images are unreachable. Doing that manually is slow and
easy to half-finish, and a half-finished pass leaves orphaned jobs pointing at a missing
user, which breaks /users/jobs for that id.

SAFETY
  - Dry-run by default. Nothing is deleted without --yes.
  - PROTECTED_OIDS can never be targeted, even if named explicitly.
  - Refuses any account that looks real unless --force-real is passed (see _looks_real).
  - SQL runs in ONE transaction with XACT_ABORT: all rows go or none do.

WHAT IT DOES NOT DO
Entra. The customer identities live in the CIAM tenant, not the resource tenant this is
authenticated against, so deleting them needs a separate `az login --tenant <ciam>`. The
script prints the exact step instead of half-doing it. ORDER MATTERS: remove the Entra
user FIRST — if the SQL row is deleted while the identity still exists, the next sign-in
recreates the row via /users/register and you are back where you started.

Usage
  python scripts/reset_test_account.py --email qa+bs01@example.com
  python scripts/reset_test_account.py --oid 8219d808-... --yes
"""
import argparse
import json
import os
import re
import subprocess
import sys

SQL_SERVER = os.environ.get("SQL_SERVER", "bettersnap-srv.database.windows.net")
SQL_DB = os.environ.get("SQL_DATABASE", "bettersnap-db")
SQL_UID = os.environ.get("SQL_UID", "CloudSAe874642e")
PWD_SECRET = os.environ.get("SQL_PASSWORD_SECRET", "Db-Password")
VAULT = os.environ.get("KEYVAULT_NAME", "bettersnapkeyvault")
STORAGE = os.environ.get("STORAGE_ACCOUNT", "bettersnapaistorage")
RG = os.environ.get("RESOURCE_GROUP", "bettersnap-ai-rg")

# Never deletable, whatever the arguments say. Add real customers here as they appear.
PROTECTED_OIDS = {
    "a036f4ea-a72d-4030-a3a2-68893f3e70c6",   # real individual, BLANK email (would be
                                              # swept up by any "accounts with no email"
                                              # rule) — 5 jobs, trained adapter
}

# Addresses that are obviously disposable/test. Anything else is treated as a real person
# and needs --force-real, because the cost of a false "test" match is deleting a customer.
#
# The house convention is a single Google Workspace ALIAS plus sub-addressing:
#     qa@bettersnap.ai  ->  qa+bs01@bettersnap.ai, qa+bs02@bettersnap.ai, ...
# Workspace ignores everything after '+', so all of those land in one inbox while Entra
# issues a distinct oid for each — unlimited throwaway accounts, no personal address, and
# it reads as a company address on a screen-share. Both the bare alias and any +suffix
# form must therefore be recognised as test accounts.
_TEST_EMAIL = re.compile(
    r"(@(example|test)\.(com|local)"      # example.com / test.local
    r"|\.invalid$"                        # RFC 2606 reserved
    r"|^exp-"                             # exp-girl-..., exp-new-...
    r"|^qa(\d+)?([+._-]|@)"               # qa@, qa01@, qa+bs01@, qa.bs01@
    r"|[.-]test\d*@"                      # kumar-test@, kumar.test@, foo-test01@ (test-auth accounts)
    r"|[+](test|qa|bs|demo)\w*@)", re.I)  # anything+test@, +qa@, +bs01@, +demo@


def sh(cmd, check=True):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if check and r.returncode != 0:
        sys.exit(f"FAILED: {cmd}\n{r.stderr.strip()}")
    return r.stdout.strip()


def sql(query, pwd):
    """-I sets QUOTED_IDENTIFIER ON: some tables carry filtered indexes that require it,
    and sqlcmd defaults it OFF (Msg 1934 on DELETE)."""
    out = sh(f'sqlcmd -S {SQL_SERVER} -d {SQL_DB} -U {SQL_UID} -P "{pwd}" -l 30 -h -1 -W -I '
             f'-Q "{query}"')
    return [l.strip() for l in out.splitlines() if l.strip() and "rows affected" not in l]


def _looks_real(email):
    return bool(email) and not _TEST_EMAIL.search(email)


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--email")
    g.add_argument("--oid")
    ap.add_argument("--yes", action="store_true", help="actually delete (default: dry run)")
    ap.add_argument("--force-real", action="store_true",
                    help="allow an address that does not look like a test account")
    a = ap.parse_args()

    pwd = sh(f'az keyvault secret show --vault-name {VAULT} --name {PWD_SECRET} '
             f'--query value -o tsv')
    key = sh(f'az storage account keys list -g {RG} -n {STORAGE} --query "[0].value" -o tsv')

    where = (f"LOWER(CAST(user_id AS varchar(50)))='{a.oid.lower()}'" if a.oid
             else f"email='{a.email}'")
    rows = sql(f"SET NOCOUNT ON; SELECT LOWER(CAST(user_id AS varchar(50))) + '|' + "
               f"ISNULL(email,'(none)') FROM dbo.users WHERE {where};", pwd)
    if not rows:
        sys.exit(f"no account matches {a.oid or a.email}")
    oid, email = rows[0].split("|", 1)

    if oid in PROTECTED_OIDS:
        sys.exit(f"REFUSING: {oid} is in PROTECTED_OIDS")
    if _looks_real(email) and not a.force_real:
        sys.exit(f"REFUSING: '{email}' does not look like a test account. "
                 f"Re-run with --force-real if you are certain.")

    # Job ids MUST be captured before the rows go — result blobs are keyed by job_id.
    jobs = sql(f"SET NOCOUNT ON; SELECT LOWER(CAST(job_id AS varchar(50))) "
               f"FROM dbo.jobs WHERE LOWER(CAST(user_id AS varchar(50)))='{oid}';", pwd)
    jobs = [j for j in jobs if re.match(r"^[0-9a-f-]{36}$", j)]

    print(f"\n  account : {oid}\n  email   : {email}\n  jobs    : {len(jobs)}")
    if not a.yes:
        print("\n  DRY RUN — nothing deleted. Re-run with --yes to apply.")
        return

    tables = ["credit_transactions", "pending_purchases", "subscriptions",
              "lora_models", "lora_trainings", "jobs", "users"]   # children -> parent
    stmts = ";".join(
        f"DELETE FROM dbo.{t} WHERE LOWER(CAST(user_id AS varchar(50)))='{oid}'"
        for t in tables)
    sql(f"SET NOCOUNT ON; SET XACT_ABORT ON; SET QUOTED_IDENTIFIER ON; "
        f"BEGIN TRANSACTION; {stmts}; COMMIT TRANSACTION;", pwd)
    print(f"  SQL     : deleted across {len(tables)} tables")

    q = f'--account-name {STORAGE} --account-key "{key}"'
    sh(f'az storage blob delete-batch {q} --source inputs --pattern "{oid}/*"', check=False)
    sh(f'az storage blob delete-batch {q} --source lora-weights '
       f'--pattern "identity/{oid}/*"', check=False)
    for j in jobs:
        sh(f'az storage blob delete-batch {q} --source outputs --pattern "results/{j}/*"',
           check=False)
        for p in (f"debug/{j}.txt", f"manifests/{j}.json"):
            sh(f'az storage blob delete {q} --container-name outputs --name "{p}"',
               check=False)
    print(f"  blobs   : inputs + adapter + {len(jobs)} job result sets")

    print("\n  STILL TO DO — Entra (different tenant, do this FIRST next time):")
    print("    az login --tenant 74e900cf-7b3a-4593-8207-deec6656d91d")
    print(f"    az ad user list --filter \"mail eq '{email}'\" --query \"[].id\" -o tsv")
    print("    az ad user delete --id <object-id>")
    print("    then purge: Entra > Users > Deleted users > Delete permanently")
    print("    (a user left in the 30-day recycle bin can return the SAME oid on re-signup)")


if __name__ == "__main__":
    main()
