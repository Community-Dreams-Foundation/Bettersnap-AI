#!/usr/bin/env bash
# Harness 2 — simulate a valid frontend upload against a local func host (curl).
#
# Usage:
#   bash local_testing/upload_test.sh test-alice ./sample.jpg
set -euo pipefail

SUB="${1:-test-alice}"
IMAGE="${2:-./sample.jpg}"
BASE_URL="${API_BASE_URL:-http://localhost:7071/api}"

[ -f "$IMAGE" ] || { echo "Image not found: $IMAGE" >&2; exit 1; }

# 1) mint a test JWT (signature not verified locally)
TOKEN="$(python local_testing/gen_test_jwt.py --sub "$SUB")"
echo "JWT sub=$SUB minted."

# 2) upload (multipart/form-data, field name MUST be 'photo')
echo "POST $BASE_URL/upload ..."
UPLOAD_RESP="$(curl -sS -X POST "$BASE_URL/upload" \
  -H "Authorization: Bearer $TOKEN" \
  -F "photo=@${IMAGE}")"
echo "$UPLOAD_RESP"

# extract input_blob_path (python to avoid a jq dependency)
INPUT_BLOB_PATH="$(printf '%s' "$UPLOAD_RESP" | python -c 'import sys,json;print(json.load(sys.stdin)["input_blob_path"])')"

# 3) submit the job
echo "POST $BASE_URL/jobs/submit ..."
curl -sS -X POST "$BASE_URL/jobs/submit" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{
        \"gender\": \"female\",
        \"age_range\": \"25-34\",
        \"hair_color\": \"brown\",
        \"purpose\": \"linkedin\",
        \"background\": \"office\",
        \"input_blob_path\": \"${INPUT_BLOB_PATH}\"
      }"
echo
