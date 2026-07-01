# Local Testing Harnesses (Windows, GPU mocked)

Two harnesses for debugging on a laptop with no GPU, against the **shared** Azure
SQL + Storage. Nothing here deploys anything.

## One-time setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install Pillow            # needed by local_test.py, not in requirements.txt
copy .env.example .env        # then fill in DB_PASSWORD + STORAGE_CONNECTION_STRING
```

> **Both laptops' public IPs must be in the Azure SQL firewall.**
> Azure portal → SQL server `bettersnap-srv` → *Networking* → *Firewall rules* →
> add each developer's current public IP (https://ifconfig.me). Without this,
> `local_test.py` and `func start` both fail to reach the DB. Public IPs change —
> re-add when your ISP rotates them.

Requires the **ODBC Driver 18 for SQL Server** installed locally (pyodbc).

---

## Harness 1 — container pipeline (`local_test.py`)

Imports and runs the **real** inference code from `..\main.py` (parent folder
`BETTERSNAP-AI_INFERENCE`) — not a reconstruction. Real SQL, real Blob; only the
GPU model is mocked.

```powershell
pip install Pillow            # torch/diffusers are stubbed, NOT needed
python local_test.py --job-id test-alice-1 --user-id test-alice
# or rely on JOB_ID/USER_ID from .env:
python local_test.py
```

What's real vs mocked:

| Step | Status |
|------|--------|
| `main.run_inference` (prompts, resize, watermark, results/<job>/ upload) | **real** |
| Azure SQL job lookup + query strings | **real** (SQL-auth, see below) |
| Blob download of the input image / upload of results | **real** |
| `torch` / `diffusers` import + `pipe(...)` GPU call | **mocked** (stubbed in `sys.modules`) |
| `load_base_model()` (`/models` + `.to("cuda")`) | **mocked** no-op |

Prod-only plumbing the harness overrides so it runs off-Azure (documented at the
top of `local_test.py`):
- `main.get_db_connection` uses `Authentication=ActiveDirectoryMsi` (managed
  identity) → replaced with this repo's SQL-auth connection (`DB_PASSWORD` from `.env`).
- `main.blob_service` uses `DefaultAzureCredential` → rebuilt from
  `STORAGE_CONNECTION_STRING` if present in `.env`, else kept as-is (needs `az login`
  + Storage Blob Data role).
- `main.update_job_status` writes to the shared DB → **dry-run (log only)** unless
  you pass `--commit`.

**Exposing the v14 silent crash:** `main.run_inference` catches each variation's
exception at `main.py:360-361` and logs only `str(e)`, then returns a possibly
empty list which `__main__` still marks `completed`. The harness flags this loudly:
if 0 images come back it errors with a pointer to those lines; if fewer than
`num_images` come back it warns. The whole orchestration is also wrapped in a
`try/except` that prints the full traceback for any pre-loop failure (DB read,
blob download, resize).

---

## Harness 2 — frontend upload against local `func start`

### Run the Function App locally

```powershell
# Install Azure Functions Core Tools v4 if needed:
#   npm i -g azure-functions-core-tools@4 --unsafe-perm true
az login                      # DefaultAzureCredential needs this for Key Vault
func start                    # serves http://localhost:7071/api/<route>
```

`local.settings.json` already supplies `AzureWebJobsStorage` + `AZURE_KEYVAULT_URL`.

### Point the frontend at localhost

Set the frontend's API base URL to `http://localhost:7071/api` (env var / config
in the frontend repo) and reload it. CORS for the local host is handled by the
Functions runtime; if the browser blocks it, add
`"Host": { "CORS": "*" }`-equivalent via `func start --cors *`.

### What the upload endpoint needs

`POST /api/upload` (see `function_app.py:78`):
- **Header:** `Authorization: Bearer <JWT>`. The JWT signature is **not verified**
  locally (`shared/auth.py:20` → `verify_signature: False`); only the `sub` claim
  (used as `user_id`) is read. So any well-formed token works — use
  `gen_test_jwt.py`.
- **Body:** `multipart/form-data` with a single field **`photo`** whose filename
  ends in `.jpg`, `.jpeg`, or `.png`.
- **Returns:** `{ url, blob_name, input_blob_path }`. Feed `input_blob_path`
  straight into `POST /api/jobs/submit`.

### Simulate a valid upload

```powershell
.\local_testing\upload_test.ps1 -Sub test-alice -Image .\sample.jpg
```
```bash
bash local_testing/upload_test.sh test-alice ./sample.jpg
```

### Why uploads may never reach the `inputs` container — check these first

1. **Key Vault access during auth.** Even though the signature isn't verified,
   `validate_token` still calls `get_secret("supabase-jwt-secret")`, which hits
   Key Vault via `DefaultAzureCredential`. If `az login` is missing or your
   account lacks Key Vault *get* permission, every authed request **401s before
   any blob write**. Fix: `az login` + grant the secret-get role, or set
   `SUPABASE_JWT_SECRET` and stub the call.
2. **Blob upload also needs Key Vault** (`shared/blob.py` →
   `get_secret("storage-connection-string")`). Same failure mode → 500.
3. **Field name / file extension.** A frontend sending `file` instead of `photo`,
   or a `.webp`, gets a `400` and nothing is uploaded.
4. **Wrong route base.** Routes are under `/api/...`. A frontend hitting
   `/upload` (no `/api`) gets a 404.

---

## Test isolation for 2 developers (shared DB + storage)

Namespace everything so Alice and Bob don't clobber each other:

| Thing            | Convention            | Example         |
|------------------|-----------------------|-----------------|
| `user_id` (sub)  | `test-<name>`         | `test-alice`    |
| `job_id`         | `test-<name>-<n>`     | `test-alice-1`  |
| input blobs      | `inputs/test-<name>/input/...` (falls out of `user_id`) | |
| output blobs     | `outputs/results/test-<name>-<n>/...` (falls out of `job_id`) | |

Because blob paths are derived from `user_id`/`job_id`, prefixing the IDs keeps
each developer's inputs and outputs in separate folders automatically. Use a
distinct `--sub` / `JOB_ID` per developer.

> The `test-<name>-<n>` `job_id` convention assumes the `jobs.job_id` column
> accepts explicit string values. If it's an `IDENTITY`/`uniqueidentifier` that
> the DB auto-generates (as `submit_job` implies via `OUTPUT INSERTED.job_id`),
> you can't force a `test-` id on the real submit path — in that case isolate via
> the `test-<name>` **user_id** and pass the DB-generated id to `local_test.py`.
> Confirm the column type before relying on string job ids.
