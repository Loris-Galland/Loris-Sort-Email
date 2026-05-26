"""Safe email deletion and archiving via Graph API batch requests.

Always moves to trash first (soft delete). Writes a JSON backup before
any bulk operation. Handles HTTP 429 rate limiting with exponential backoff.
"""

# Phase 5 — implemented in the actions phase

# Graph API batch endpoint: POST /v1.0/$batch (max 20 operations per request)
# Soft delete: move to "Deleted Items" folder, never permanent delete directly
# Rate limit: 10k requests / 10 min -> backoff 2^n seconds on 429
