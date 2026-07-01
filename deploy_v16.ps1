<#
  deploy_v16.ps1 — BetterSnap AI inference deploy (everything EXCEPT triggering a job).

  Run this in your VS Code terminal (where `az` is logged in), or paste it to
  Claude Code to execute. It is intentionally SAFE: it never starts an A100 job.
  The only GPU-spending action — `az containerapp job start` — is left out on
  purpose. You trigger the test run yourself, separately, when ready.

  Steps:
    0. Preflight — confirm az login + required CLIs
    1. Apply DB migrations 001 + 002 (idempotent; pulls DB password from Key Vault)
    2. Build + push inference:v16 to ACR
    3. Point the job at inference:v16
    4. Publish the Functions backend (dispatch + refund + result-url)
    5. Verify image + GPU profile

  Usage:
    ./deploy_v16.ps1 -FunctionApp "<your-function-app-name>"
    # add -SkipMigrations if you applied 001/002 in the portal already
#>

param(
  [Parameter(Mandatory = $true)]  [string]$FunctionApp,
  [string]$ResourceGroup = "bettersnap-ai-rg",
  [string]$JobName       = "bettersnapai-if",
  [string]$Registry      = "bettersnapregistry",
  [string]$ImageRepo     = "bettersnapregistry-gta3hah3g3bpgrcn.azurecr.io/inference",
  [string]$ImageTag      = "v16",
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

# ── 1. DB migrations (idempotent) ───────────────────────────────────────────
if (-not $SkipMigrations) {
  Step 1 "Apply DB migrations 001 + 002"
  if (-not (Get-Command sqlcmd -ErrorAction SilentlyContinue)) {
    Write-Warning "sqlcmd not found. Either install it (https://aka.ms/sqlcmd) or"
    Write-Warning "apply migrations/001_gpu_dispatch_lease.sql and 002_jobs_dispatch_idempotency.sql"
    Write-Warning "via the Azure portal query editor, then re-run with -SkipMigrations."
    Confirm-Continue "Continue WITHOUT applying migrations? (dispatch will fail-closed if they're not applied)"
  } else {
    $dbpwd = az keyvault secret show --vault-name $KeyVault --name "Db-Password" --query "value" -o tsv
    if (-not $dbpwd) { Write-Error "Could not read Db-Password from Key Vault $KeyVault"; exit 1 }
    foreach ($m in @("migrations/001_gpu_dispatch_lease.sql", "migrations/002_jobs_dispatch_idempotency.sql")) {
      $path = Join-Path $RepoRoot "Bettersnap-aI_Backend/$m"
      Write-Host "Applying $m ..."
      sqlcmd -S $SqlServer -d $SqlDatabase -U $SqlUser -P $dbpwd -b -i $path
    }
    Write-Host "Verifying lease singleton row ..."
    sqlcmd -S $SqlServer -d $SqlDatabase -U $SqlUser -P $dbpwd -b `
      -Q "SELECT lease_name, owner_id, expires_at FROM dbo.GpuDispatchLease;"
  }
} else {
  Step 1 "Migrations SKIPPED (-SkipMigrations)"
}

# ── 2. Build + push v16 ─────────────────────────────────────────────────────
Step 2 "Build + push $Image (ACR build)"
Confirm-Continue "Build and push $ImageTag from $RepoRoot ?"
az acr build --registry $Registry --image "inference:$ImageTag" $RepoRoot

# ── 3. Point the job at v16 ─────────────────────────────────────────────────
Step 3 "Update job $JobName -> $Image"
az containerapp job update --name $JobName --resource-group $ResourceGroup --image $Image | Out-Null

# ── 4. Publish Functions backend ────────────────────────────────────────────
Step 4 "Publish Functions app '$FunctionApp'"
Confirm-Continue "Publish backend from Bettersnap-aI_Backend to $FunctionApp ?"
Push-Location (Join-Path $RepoRoot "Bettersnap-aI_Backend")
try { func azure functionapp publish $FunctionApp --python }
finally { Pop-Location }

# ── 5. Verify ───────────────────────────────────────────────────────────────
Step 5 "Verify image + GPU profile"
az containerapp job show --name $JobName --resource-group $ResourceGroup `
  --query "{image:properties.template.containers[0].image, profile:properties.workloadProfileName}" -o table

Write-Host "`nDONE. Backend + image deployed. No job was triggered." -ForegroundColor Green
Write-Host "To run the ONE manual test (yourself, when ready):" -ForegroundColor Yellow
Write-Host "  1) Reset the row to 'queued' (NOT 'failed', or the new guard no-ops it):" -ForegroundColor Yellow
Write-Host "     UPDATE jobs SET status='queued', completed_at=NULL, external_execution_id=NULL" -ForegroundColor DarkGray
Write-Host "     WHERE job_id='C91E1355-D66A-4A68-90FC-8B36160143F7';" -ForegroundColor DarkGray
Write-Host "  2) az containerapp job start -n $JobName -g $ResourceGroup \`" -ForegroundColor DarkGray
Write-Host "       --env-vars JOB_ID=C91E1355-D66A-4A68-90FC-8B36160143F7 USER_ID=B1274FF7-8F9F-48E6-81A4-E5BDCBC993B0" -ForegroundColor DarkGray
Write-Host "  Then bring the total_memory + INFERENCE VRAM PEAK numbers back to review." -ForegroundColor Yellow
