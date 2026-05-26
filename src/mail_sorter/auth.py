"""OAuth2 PKCE authentication against Microsoft Graph API.

Handles the full auth flow: open browser, receive callback on localhost:8080,
exchange code for tokens, store encrypted via OS keyring. No client_secret needed.
"""

import base64
import contextlib
import hashlib
import json
import os
import secrets
import time
import webbrowser
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
import keyring
import keyring.errors
import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

load_dotenv()

KEYRING_SERVICE = "mail-sorter"
KEYRING_TOKEN_KEY = "ms_token"
CALLBACK_PORT = 8080
CALLBACK_PATH = "/callback"

# Minimum scopes — do not add more
SCOPES = ["Mail.Read", "Mail.ReadWrite", "offline_access"]

AUTH_BASE_URL = "https://login.microsoftonline.com"

console = Console()
auth_app = typer.Typer(help="Login / logout from Microsoft Outlook")


def _generate_pkce_pair() -> tuple[str, str]:
    """Generate (code_verifier, code_challenge) for PKCE flow.

    code_verifier: 64 random bytes encoded as base64url (86 chars, within RFC 43-128 limit).
    code_challenge: SHA256(code_verifier) encoded as base64url.
    """
    code_verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode()
    digest = hashlib.sha256(code_verifier.encode()).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return code_verifier, code_challenge


def _build_auth_url(client_id: str, tenant_id: str, code_challenge: str, state: str) -> str:
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": f"http://localhost:{CALLBACK_PORT}{CALLBACK_PATH}",
        "response_mode": "query",
        "scope": " ".join(SCOPES),
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "prompt": "select_account",
    }
    return f"{AUTH_BASE_URL}/{tenant_id}/oauth2/v2.0/authorize?{urlencode(params)}"


class _CallbackHandler(BaseHTTPRequestHandler):
    """Temporary HTTP server that receives the OAuth2 authorization code."""

    auth_code: str | None = None
    auth_state: str | None = None
    error: str | None = None

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != CALLBACK_PATH:
            self._respond(404, "Not found")
            return

        params = parse_qs(parsed.query)

        if "error" in params:
            _CallbackHandler.error = params.get("error_description", ["Unknown error"])[0]
            self._respond(400, f"Authentication error: {_CallbackHandler.error}")
            return

        _CallbackHandler.auth_code = params.get("code", [None])[0]
        _CallbackHandler.auth_state = params.get("state", [None])[0]
        self._respond(200, "Login successful. You can close this tab.")

    def _respond(self, code: int, message: str) -> None:
        body = (
            f"<html><body style='font-family:sans-serif;text-align:center;padding:60px'>"
            f"<h2>{message}</h2></body></html>"
        ).encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        pass  # suppress HTTP server logs


def _wait_for_callback(timeout: int = 120) -> tuple[str | None, str | None, str | None]:
    """Start a local HTTP server and wait for the OAuth2 callback.

    Polls every 1 second until a code or error arrives, or timeout is reached.
    Returns (auth_code, state, error_message).
    """
    _CallbackHandler.auth_code = None
    _CallbackHandler.auth_state = None
    _CallbackHandler.error = None

    server = HTTPServer(("localhost", CALLBACK_PORT), _CallbackHandler)
    server.timeout = 1  # non-blocking: handle one request per second

    start = time.monotonic()
    while time.monotonic() - start < timeout:
        server.handle_request()
        if _CallbackHandler.auth_code or _CallbackHandler.error:
            break

    server.server_close()
    return _CallbackHandler.auth_code, _CallbackHandler.auth_state, _CallbackHandler.error


def _exchange_code_for_token(
    client_id: str, tenant_id: str, code: str, code_verifier: str
) -> dict:
    """Exchange the authorization code for access + refresh tokens.

    No client_secret needed — the code_verifier proves we initiated the flow.
    """
    response = httpx.post(
        f"{AUTH_BASE_URL}/{tenant_id}/oauth2/v2.0/token",
        data={
            "client_id": client_id,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": f"http://localhost:{CALLBACK_PORT}{CALLBACK_PATH}",
            "code_verifier": code_verifier,
            "scope": " ".join(SCOPES),
        },
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def _refresh_access_token(client_id: str, tenant_id: str, refresh_token: str) -> dict:
    """Silently renew an expired access token using the refresh token.

    Microsoft personal account refresh tokens are valid for 90 days.
    If this fails, the user needs to log in again.
    """
    response = httpx.post(
        f"{AUTH_BASE_URL}/{tenant_id}/oauth2/v2.0/token",
        data={
            "client_id": client_id,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "scope": " ".join(SCOPES),
        },
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def save_token(token_data: dict) -> None:
    """Store tokens in the OS keyring (Windows Credential Manager).

    Adds an expires_at field so we can check expiry without a network call.
    60s margin to avoid using a token that's about to expire.
    """
    # guard against malformed responses where expires_in < 60
    expires_in = max(token_data.get("expires_in", 3600), 61)
    expires_at = datetime.now(UTC) + timedelta(seconds=expires_in - 60)
    token_data["expires_at"] = expires_at.isoformat()
    keyring.set_password(KEYRING_SERVICE, KEYRING_TOKEN_KEY, json.dumps(token_data))


def load_token() -> dict | None:
    raw = keyring.get_password(KEYRING_SERVICE, KEYRING_TOKEN_KEY)
    return json.loads(raw) if raw else None


def delete_token() -> None:
    with contextlib.suppress(keyring.errors.PasswordDeleteError):
        keyring.delete_password(KEYRING_SERVICE, KEYRING_TOKEN_KEY)


def get_valid_access_token() -> str | None:
    """Return a valid access token, refreshing silently if expired.

    Returns None if not logged in or if the refresh token has expired.
    """
    token_data = load_token()
    if not token_data:
        return None

    expires_at = datetime.fromisoformat(token_data["expires_at"])
    if datetime.now(UTC) < expires_at:
        return token_data["access_token"]

    # Token expired — try a silent refresh
    client_id = os.getenv("AZURE_CLIENT_ID", "")
    tenant_id = os.getenv("AZURE_TENANT_ID", "common")
    try:
        new_token = _refresh_access_token(client_id, tenant_id, token_data["refresh_token"])
        save_token(new_token)
        return new_token["access_token"]
    except httpx.HTTPStatusError:
        # Refresh token expired (>90 days inactive) — need to log in again
        delete_token()
        return None


@auth_app.command("login")
def login() -> None:
    """Log in to Microsoft Outlook via OAuth2 PKCE."""
    client_id = os.getenv("AZURE_CLIENT_ID", "")
    tenant_id = os.getenv("AZURE_TENANT_ID", "common")

    if not client_id:
        console.print("[red]AZURE_CLIENT_ID missing from .env[/red]")
        console.print("See the README for how to create a free Azure App Registration.")
        raise typer.Exit(1)

    if get_valid_access_token():
        console.print("Already logged in. Run 'auth logout' first if you want to switch accounts.")
        raise typer.Exit(0)

    code_verifier, code_challenge = _generate_pkce_pair()
    state = secrets.token_urlsafe(16)  # CSRF protection

    auth_url = _build_auth_url(client_id, tenant_id, code_challenge, state)

    with console.status("Opening browser..."):
        time.sleep(0.5)
        webbrowser.open(auth_url)

    console.print("Waiting for Microsoft callback on localhost:8080 (2 min timeout)...")

    code, received_state, error = _wait_for_callback(timeout=120)

    if error:
        console.print(f"[red]Authentication error:[/red] {error}")
        raise typer.Exit(1)

    if not code:
        console.print("[red]Timeout — no response received.[/red]")
        console.print(
            "Make sure http://localhost:8080/callback is registered as a redirect URI "
            "in your Azure App Registration."
        )
        raise typer.Exit(1)

    if received_state != state:
        console.print("[red]Security error: state mismatch (possible CSRF). Try again.[/red]")
        raise typer.Exit(1)

    with console.status("Exchanging code for tokens..."):
        try:
            token_data = _exchange_code_for_token(client_id, tenant_id, code, code_verifier)
        except httpx.HTTPStatusError as exc:
            body = exc.response.json() if exc.response.content else {}
            err = body.get("error_description", str(exc))
            console.print(f"[red]Token exchange failed:[/red] {err}")
            raise typer.Exit(1) from exc

    save_token(token_data)
    console.print("[green]Logged in successfully.[/green]")
    _print_account_info(token_data["access_token"])


@auth_app.command("status")
def status() -> None:
    """Check if you are logged in and display account info."""
    token = get_valid_access_token()
    if not token:
        console.print("Not logged in. Run: mail-sorter auth login")
        raise typer.Exit(0)
    _print_account_info(token)


@auth_app.command("logout")
def logout() -> None:
    """Remove the stored token from the OS keyring."""
    if not load_token():
        console.print("Not logged in.")
        raise typer.Exit(0)
    delete_token()
    console.print("[green]Logged out.[/green] Local data (SQLite) was not deleted.")


def _print_account_info(access_token: str) -> None:
    """Fetch /me from Graph API and display name + email in a Rich table."""
    try:
        resp = httpx.get(
            "https://graph.microsoft.com/v1.0/me",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        resp.raise_for_status()
        me = resp.json()

        table = Table(show_header=False, border_style="green")
        table.add_column(style="cyan", min_width=10)
        table.add_column(style="white")
        table.add_row("Name", me.get("displayName") or "—")
        table.add_row("Email", me.get("mail") or me.get("userPrincipalName") or "—")
        table.add_row("Status", "[green]Connected[/green]")
        console.print(table)
    except httpx.HTTPStatusError:
        console.print("[green]Connected[/green] (could not fetch profile details)")
