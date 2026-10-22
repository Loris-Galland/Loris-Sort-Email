"""Microsoft Graph API client, metadata only.

Every message request uses $select so only allowed fields are returned.
Never requests body content or attachment data.
"""

import time
from collections.abc import Callable, Iterator

import httpx

from mail_sorter.auth import get_valid_access_token

GRAPH_URL = "https://graph.microsoft.com/v1.0"

# The only message fields we ever ask for. Never add body, bodyPreview or attachments.
MESSAGE_FIELDS = "id,subject,from,receivedDateTime,isRead,hasAttachments,importance"

# Message size is not a regular Graph field, only a MAPI property (PR_MESSAGE_SIZE)
SIZE_PROPERTY = "Integer 0x0E08"

# Immutable IDs survive a move between folders, which makes restore possible
PREFER_IMMUTABLE_ID = 'IdType="ImmutableId"'

PAGE_SIZE = 500
# Outlook runs at most 4 requests at once per mailbox, a bigger $batch only gets 429s
BATCH_SIZE = 4
MAX_RETRIES = 6

# Replaced in tests so retries do not actually wait
sleep = time.sleep


class NotLoggedInError(Exception):
    pass


class GraphError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(f"HTTP {status_code}: {message}")
        self.status_code = status_code


def _retry_delay(retry_after: str | None, attempt: int) -> float:
    if retry_after and str(retry_after).isdigit():
        return float(retry_after)
    return float(2**attempt)


def _error_message(response: httpx.Response) -> str:
    try:
        return response.json()["error"]["message"]
    except (ValueError, KeyError, TypeError):
        return response.text[:200]


def _folders_url(parent_id: str | None) -> str:
    return f"/me/mailFolders/{parent_id}/childFolders" if parent_id else "/me/mailFolders"


class GraphClient:
    def __init__(
        self,
        token_provider: Callable[[], str | None] = get_valid_access_token,
        http: httpx.Client | None = None,
    ):
        self._token_provider = token_provider
        self._http = http or httpx.Client(timeout=60)
        self._folder_cache: dict[str, str] = {}

    def _headers(self) -> dict[str, str]:
        token = self._token_provider()
        if not token:
            raise NotLoggedInError("Not logged in. Run: mail-sorter auth login")
        return {"Authorization": f"Bearer {token}", "Prefer": PREFER_IMMUTABLE_ID}

    def request(self, method: str, url: str, **kwargs) -> httpx.Response:
        """Send a request, retrying on throttling (429) and transient server errors."""
        if not url.startswith("http"):
            url = GRAPH_URL + url
        response = None
        for attempt in range(MAX_RETRIES):
            response = self._http.request(method, url, headers=self._headers(), **kwargs)
            if response.status_code in (429, 502, 503, 504):
                sleep(_retry_delay(response.headers.get("Retry-After"), attempt))
                continue
            if response.status_code >= 400:
                raise GraphError(response.status_code, _error_message(response))
            return response
        raise GraphError(response.status_code, "too many retries")

    def _paginate(self, url: str, params: dict) -> Iterator[dict]:
        while url:
            data = self.request("GET", url, params=params).json()
            yield from data.get("value", [])
            # nextLink already carries every query parameter
            url = data.get("@odata.nextLink")
            params = None

    def inbox_count(self) -> int:
        resp = self.request("GET", "/me/mailFolders/inbox", params={"$select": "totalItemCount"})
        return resp.json().get("totalItemCount", 0)

    def iter_inbox(self) -> Iterator[dict]:
        """Yield the metadata of every message in the inbox."""
        params = {
            "$select": MESSAGE_FIELDS,
            "$top": str(PAGE_SIZE),
            "$expand": f"singleValueExtendedProperties($filter=id eq '{SIZE_PROPERTY}')",
        }
        yield from self._paginate("/me/mailFolders/inbox/messages", params)

    def ensure_folder(self, path: str) -> str:
        """Return the ID of a folder like "Jeux/Riot", creating missing levels."""
        key = path.strip("/").lower()
        if key in self._folder_cache:
            return self._folder_cache[key]

        parent_id = None
        for name in [part.strip() for part in path.strip("/").split("/") if part.strip()]:
            children = self._paginate(_folders_url(parent_id), {"$select": "id,displayName"})
            match = next((f for f in children if f["displayName"].lower() == name.lower()), None)
            if match:
                parent_id = match["id"]
            else:
                created = self.request("POST", _folders_url(parent_id), json={"displayName": name})
                parent_id = created.json()["id"]

        if parent_id is None:
            raise ValueError(f"Invalid folder path: {path!r}")
        self._folder_cache[key] = parent_id
        return parent_id

    def move_messages(
        self,
        message_ids: list[str],
        destination: str,
        on_progress: Callable[[int], None] | None = None,
    ) -> dict[str, str | None]:
        """Move messages using $batch. Returns {old_id: new_id}, new_id is None on failure.

        destination is a folder ID or a well-known name such as "deleteditems" or "inbox".
        """
        results: dict[str, str | None] = {}
        for start in range(0, len(message_ids), BATCH_SIZE):
            chunk = message_ids[start : start + BATCH_SIZE]
            results.update(self._move_chunk(chunk, destination))
            if on_progress:
                on_progress(len(chunk))
        return results

    def _move_chunk(self, ids: list[str], destination: str) -> dict[str, str | None]:
        pending = dict(enumerate(ids))
        results: dict[str, str | None] = {}
        for attempt in range(MAX_RETRIES):
            requests = [
                {
                    "id": str(i),
                    "method": "POST",
                    "url": f"/me/messages/{msg_id}/move",
                    "body": {"destinationId": destination},
                    "headers": {"Content-Type": "application/json", "Prefer": PREFER_IMMUTABLE_ID},
                }
                for i, msg_id in pending.items()
            ]
            resp = self.request("POST", "/$batch", json={"requests": requests})

            throttled = {}
            delay = 0.0
            for item in resp.json().get("responses", []):
                i = int(item["id"])
                status = item.get("status", 500)
                if status == 429 or status >= 500:
                    throttled[i] = pending[i]
                    retry_after = (item.get("headers") or {}).get("Retry-After")
                    delay = max(delay, _retry_delay(retry_after, attempt))
                elif status < 300:
                    # only the new ID is kept from the response, the rest is discarded
                    results[pending[i]] = (item.get("body") or {}).get("id", pending[i])
                else:
                    results[pending[i]] = None

            if not throttled:
                break
            pending = throttled
            sleep(delay)

        for msg_id in pending.values():
            results.setdefault(msg_id, None)
        return results
