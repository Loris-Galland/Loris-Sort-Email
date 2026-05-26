"""Microsoft Graph API client — metadata only.

Fetches email metadata using $select to ensure only allowed fields are returned.
Never requests body content or attachment data.
"""

# Phase 2 — implemented in the indexing phase

# Graph API endpoint: GET /me/messages
#   ?$select=id,subject,from,receivedDateTime,isRead,hasAttachments,importance
#   &$top=100         <- max per request allowed by Graph API
#   &$orderby=receivedDateTime desc
#
# Pagination: follow @odata.nextLink until None
# Rate limiting: exponential backoff on HTTP 429 (10k req / 10 min quota)
