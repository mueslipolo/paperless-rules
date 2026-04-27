#!/usr/bin/env bash
# Drop-in replacement for paperless-ngx PAPERLESS_POST_CONSUME_SCRIPT.
#
# Mount this file into the paperless container under /usr/src/paperless/scripts/
# and point PAPERLESS_POST_CONSUME_SCRIPT at it. paperless calls the script
# with $DOCUMENT_ID set; we forward that to paperless-rules' webhook so the
# rules engine applies metadata in-process. Always exits 0 — never break
# paperless's pipeline because of a downstream rule failure.
#
# Required env (set on the paperless service):
#   DOCUMENT_ID         injected by paperless on each consume
#   PAPERLESS_RULES_TOKEN   same paperless API token paperless-rules uses
# Optional env:
#   PAPERLESS_RULES_URL     defaults to http://paperless-rules:8765
#                           (works inside the docker network; override for
#                           non-default service names)

set -uo pipefail
: "${DOCUMENT_ID:?missing DOCUMENT_ID}"
: "${PAPERLESS_RULES_TOKEN:?missing PAPERLESS_RULES_TOKEN}"
: "${PAPERLESS_RULES_URL:=http://paperless-rules:8765}"

curl --max-time 30 -fsSL \
    -X POST "${PAPERLESS_RULES_URL}/api/post-consume" \
    -H "Content-Type: application/json" \
    -H "Authorization: Token ${PAPERLESS_RULES_TOKEN}" \
    -d "{\"doc_id\": ${DOCUMENT_ID}}" \
    >/dev/null \
    || echo "paperless-rules: post-consume call failed for doc ${DOCUMENT_ID} (non-fatal)" >&2

exit 0
