# Harness 2 — simulate a valid frontend upload against a local func host.
#
# Flow mirrors the real frontend:
#   1) POST /api/upload   (multipart: field "photo")  -> input_blob_path
#   2) POST /api/jobs/submit (JSON)                    -> job_id
#
# Prereqs:
#   - `func start` running (see LOCAL_TESTING.md)
#   - a test image at the path below
#   - pyjwt installed (pip install -r requirements.txt)
#
# Usage:
#   .\local_testing\upload_test.ps1 -Sub test-alice -Image .\sample.jpg

param(
    [string]$Sub = "test-alice",
    [string]$Image = ".\sample.jpg",
    [string]$BaseUrl = "http://localhost:7071/api"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $Image)) { throw "Image not found: $Image" }

# 1) mint a test JWT (signature not verified locally)
$token = python local_testing/gen_test_jwt.py --sub $Sub
Write-Host "JWT sub=$Sub minted." -ForegroundColor Cyan

# 2) upload the photo (multipart/form-data, field name MUST be 'photo')
Write-Host "POST $BaseUrl/upload ..." -ForegroundColor Cyan
$form = @{ photo = Get-Item $Image }
$uploadResp = Invoke-RestMethod -Uri "$BaseUrl/upload" -Method Post `
    -Headers @{ Authorization = "Bearer $token" } -Form $form
$uploadResp | ConvertTo-Json
$inputBlobPath = $uploadResp.input_blob_path

# 3) submit a job referencing that input_blob_path
Write-Host "POST $BaseUrl/jobs/submit ..." -ForegroundColor Cyan
$body = @{
    gender          = "female"
    age_range       = "25-34"
    hair_color      = "brown"
    purpose         = "linkedin"
    background      = "office"
    input_blob_path = $inputBlobPath
} | ConvertTo-Json

$submitResp = Invoke-RestMethod -Uri "$BaseUrl/jobs/submit" -Method Post `
    -Headers @{ Authorization = "Bearer $token"; "Content-Type" = "application/json" } `
    -Body $body
$submitResp | ConvertTo-Json
Write-Host "job_id=$($submitResp.job_id)" -ForegroundColor Green
