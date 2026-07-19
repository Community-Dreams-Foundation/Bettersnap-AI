<#
  deploy.ps1 — BetterSnap AI inference deploy (everything EXCEPT triggering a job).

  Canonical image tag: v33 — SDXL txt2img + per-user LoRA generation (Aragon-style), baked ohwx trigger.
  job.yaml and this script are kept in lock-step on this tag; bump -ImageTag here
  AND update job.yaml together, never one alone.

  Run this in your VS Code terminal (where `az` is logged in), or paste it to
  Claude Code to execute. It is intentionally SAFE: it never starts an A100 job.
  The only GPU-spending action — `az containerapp job start` — is left out on
  purpose. You trigger the test run yourself, separately, when ready.

  Steps:
    0. Preflight — confirm az login + required CLIs
    1. Apply ALL pending DB migrations via the versioned runner (idempotent; tracks
       dbo.schema_migrations; discovers 001..NNN automatically — never a hardcoded list)
    2. Build + push inference:v23 to ACR
    3. Point the job at inference:v23 AND set its A100 resources (cpu + memory)
    4. Publish the Functions backend (dispatch + refund + result-url)
    5. Verify image + GPU profile

  Usage:
    ./deploy.ps1 -FunctionApp "<your-function-app-name>"
    # add -SkipMigrations if you applied 001/002 in the portal already
#>

param(
  [Parameter(Mandatory = $true)]  [string]$FunctionApp,
  [string]$ResourceGroup = "bettersnap-ai-rg",
  [string]$JobName       = "bettersnapai-if",
  [string]$Registry      = "bettersnapregistry",
  [string]$ImageRepo     = "bettersnapregistry-gta3hah3g3bpgrcn.azurecr.io/inference",
  # Canonical tag. v33 = SDXL txt2img per-user LoRA GENERATION (Aragon/BetterPic
  # model): gender-driven attire + subject noun, robust age mapping, demographic-
  # neutral negatives, one job spans ~6 (background,attire) tuples, and the baked
  # `ohwx {subject}` trigger so each user's identity LoRA fires by default.
  # MUST match the image tag in job.yaml — bump both together or ACA can serve a
  # stale image. (History: v23 = SDXL img2img good; v30 = + identity LoRA;
  # v31 = + depth-ControlNet; v32 = txt2img rework; v33 = + baked ohwx trigger.)
  [string]$ImageTag      = "v35",
  # A100 resource alloc, set on every redeploy (see Step 3). These MUST match the
  # Consumption-GPU-NC24-A100 workload profile's fixed alloc AND job.yaml, or a
  # bare `--image`-only update silently drops the job toward the 1Gi OOM default.
  [string]$Cpu           = "24",
  [string]$Memory        = "220Gi",
  [string]$KeyVault      = "bettersnapkeyvault",
  [string]$SqlServer     = "bettersnap-srv.database.windows.net",
  [string]$SqlDatabase   = "bettersnap-db",
  [string]$SqlUser       = "CloudSAe874642e",
  [switch]$SkipMigrations
)

$ErrorActionPreference = "Stop"
$RepoRoot = $PSScriptRoot
$Image    = "$ImageRepo`:$ImageTag"

function Step($n, $msg) { Write-Host "`n=== [$n] $msg ===" -ForegroundColor Cyan }
function Confirm-Continue($msg) {
  $r = Read-Host "$msg  [y/N]"
  if ($r -ne "y") { Write-Host "Stopped." -ForegroundColor Yellow; exit 1 }
}

# ── 0. Preflight ────────────────────────────────────────────────────────────
Step 0 "Preflight"
$acct = az account show --query "user.name" -o tsv 2>$null
if (-not $acct) { Write-Error "Not logged in. Run 'az login' first."; exit 1 }
Write-Host "az account: $acct"
foreach ($cli in @("az")) {
  if (-not (Get-Command $cli -ErrorAction SilentlyContinue)) { Write-Error "$cli not found"; exit 1 }
}

# ── 1. DB migrations (versioned, idempotent) ────────────────────────────────
# Applies EVERY pending migrations/NNN_*.sql via scripts/run_migrations.py, which tracks
# applied files in dbo.schema_migrations and only runs what is new. This REPLACED a
# hardcoded 001/002/003 sqlcmd loop that silently skipped 004+ — the exact reason the
# schema could drift behind the code ("004-010 never applied by the deploy"). The runner
# discovers migrations automatically, so a new migrations/014_*.sql is picked up with no
# edit to this script. Runs FIRST, so no code ever ships before the columns it reads.
# Auth: the runner reads the DB password from Key Vault via DefaultAzureCredential, i.e.
# the same `az login` confirmed in preflight (no password handling in this script).
if (-not $SkipMigrations) {
  Step 1 "Apply pending DB migrations (versioned runner)"
  if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Error "python not found — required for scripts/run_migrations.py. Install Python 3, or"
    Write-Error "apply pending migrations out-of-band and re-run with -SkipMigrations."
    exit 1
  }
  Push-Location (Join-Path $RepoRoot "Bettersnap-aI_Backend")
  try {
    Write-Host "Pending migrations:"
    python scripts/run_migrations.py --dry-run
    if ($LASTEXITCODE -ne 0) { Write-Error "Could not reach the DB to check migrations ($LASTEXITCODE)."; exit 1 }
    python scripts/run_migrations.py
    if ($LASTEXITCODE -ne 0) { Write-Error "run_migrations.py failed ($LASTEXITCODE) — schema not up to date, refusing to deploy code."; exit 1 }
  } finally { Pop-Location }
} else {
  Step 1 "Migrations SKIPPED (-SkipMigrations)"
}

# ── 2. Build + push v23 ─────────────────────────────────────────────────────
Step 2 "Build + push $Image (ACR build)"
Confirm-Continue "Build and push $ImageTag from $RepoRoot ?"
az acr build --registry $Registry --image "inference:$ImageTag" $RepoRoot

# ── 3. Point the job at v23 + set A100 resources ────────────────────────────
# --cpu/--memory are set here too, NOT just --image. An image-only update leaves
# resources at whatever the job was last created with; if that ever regresses to
# the ~1Gi default the container OOM-SIGKILLs (exit 137) on model load. Setting
# them every deploy makes the 220Gi alloc self-healing.
Step 3 "Update job $JobName -> $Image (cpu=$Cpu memory=$Memory)"
az containerapp job update --name $JobName --resource-group $ResourceGroup `
  --image $Image --cpu $Cpu --memory $Memory | Out-Null

# ── 4. Publish Functions backend ────────────────────────────────────────────
Step 4 "Publish Functions app '$FunctionApp'"
Confirm-Continue "Publish backend from Bettersnap-aI_Backend to $FunctionApp ?"
Push-Location (Join-Path $RepoRoot "Bettersnap-aI_Backend")
try { func azure functionapp publish $FunctionApp --python }
finally { Pop-Location }

# ── 5. Verify ───────────────────────────────────────────────────────────────
Step 5 "Verify image + GPU profile"
az containerapp job show --name $JobName --resource-group $ResourceGroup `
  --query "{image:properties.template.containers[0].image, cpu:properties.template.containers[0].resources.cpu, memory:properties.template.containers[0].resources.memory, profile:properties.workloadProfileName}" -o table

Write-Host "`nDONE. Backend + image deployed. No job was triggered." -ForegroundColor Green
Write-Host "To run the ONE manual test (yourself, when ready):" -ForegroundColor Yellow
Write-Host "  1) Reset the row to 'queued' (NOT 'failed', or the new guard no-ops it):" -ForegroundColor Yellow
Write-Host "     UPDATE jobs SET status='queued', completed_at=NULL, external_execution_id=NULL" -ForegroundColor DarkGray
Write-Host "     WHERE job_id='C91E1355-D66A-4A68-90FC-8B36160143F7';" -ForegroundColor DarkGray
Write-Host "  2) az containerapp job start -n $JobName -g $ResourceGroup \" -ForegroundColor DarkGray
Write-Host "       --env-vars JOB_ID=C91E1355-D66A-4A68-90FC-8B36160143F7 USER_ID=B1274FF7-8F9F-48E6-81A4-E5BDCBC993B0" -ForegroundColor DarkGray
Write-Host "  Then bring the total_memory + INFERENCE VRAM PEAK numbers back to review." -ForegroundColor Yellow
