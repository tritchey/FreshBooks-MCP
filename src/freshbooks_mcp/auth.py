"""FreshBooks OAuth2 authorization-code flow.

FreshBooks issues single-use refresh tokens: exactly one refresh token is alive
per user per application, and using it invalidates it. So the new pair is
persisted the instant a refresh succeeds, and a failed refresh must leave the
stored tokens completely alone -- they may still be the only valid pair.
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlencode

import httpx

from . import store

AUTHORIZE_URL = "https://auth.freshbooks.com/oauth/authorize/"
TOKEN_URL = "https://api.freshbooks.com/auth/oauth/token"

# Refresh this many seconds before the access token actually expires.
EXPIRY_MARGIN_SECONDS = 60
TIMEOUT_SECONDS = 30.0


class AuthError(RuntimeError):
    """Raised when authorization or a token exchange/refresh fails."""


def build_auth_url() -> str:
    """Return the FreshBooks consent URL for this app."""
    creds = store.get_app_credentials()
    query = urlencode(
        {
            "response_type": "code",
            "redirect_uri": creds["redirect_uri"],
            "client_id": creds["client_id"],
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


def _post_token(payload: dict[str, str], transport: httpx.BaseTransport | None = None) -> dict[str, Any]:
    with httpx.Client(transport=transport, timeout=TIMEOUT_SECONDS) as client:
        response = client.post(
            TOKEN_URL,
            json=payload,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
    if response.status_code < 200 or response.status_code >= 300:
        raise AuthError(
            f"FreshBooks token request failed ({payload['grant_type']}, "
            f"HTTP {response.status_code}): {response.text}"
        )
    try:
        return response.json()
    except ValueError as exc:
        raise AuthError(f"FreshBooks token response was not JSON: {response.text}") from exc


def _persist(payload: dict[str, Any]) -> dict[str, Any]:
    """Merge a token response into tokens.json (atomically) and return the result."""
    for field in ("access_token", "refresh_token"):
        if not payload.get(field):
            raise AuthError(f"FreshBooks token response is missing '{field}': {payload}")

    expires_in = float(payload.get("expires_in") or 0)
    issued_at = float(payload["created_at"]) if payload.get("created_at") else time.time()

    tokens = store.load_tokens()
    tokens.update(
        {
            "access_token": payload["access_token"],
            "refresh_token": payload["refresh_token"],
            "expires_at": issued_at + expires_in,
        }
    )
    store.save_tokens(tokens)
    return tokens


def exchange_code(code: str, transport: httpx.BaseTransport | None = None) -> dict[str, Any]:
    """Trade an authorization code for tokens and persist them before returning."""
    creds = store.get_app_credentials()
    payload = _post_token(
        {
            "grant_type": "authorization_code",
            "client_id": creds["client_id"],
            "client_secret": creds["client_secret"],
            "redirect_uri": creds["redirect_uri"],
            "code": code,
        },
        transport=transport,
    )
    return _persist(payload)


def refresh_tokens(transport: httpx.BaseTransport | None = None) -> dict[str, Any]:
    """Exchange the stored refresh token for a new pair, persisting it immediately."""
    creds = store.get_app_credentials()
    tokens = store.load_tokens()
    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        raise AuthError(
            "No FreshBooks refresh token stored. Run get_auth_url and submit_auth_code to connect."
        )

    # _post_token raises on any non-2xx without touching tokens.json, which is
    # what keeps a transient failure from destroying a still-valid token pair.
    payload = _post_token(
        {
            "grant_type": "refresh_token",
            "client_id": creds["client_id"],
            "client_secret": creds["client_secret"],
            "redirect_uri": creds["redirect_uri"],
            "refresh_token": refresh_token,
        },
        transport=transport,
    )
    return _persist(payload)


def ensure_access_token(transport: httpx.BaseTransport | None = None) -> str:
    """Return a usable access token, refreshing if it expires within a minute."""
    tokens = store.load_tokens()
    if not tokens.get("access_token"):
        raise AuthError(
            "Not connected to FreshBooks. Call get_auth_url, approve access, "
            "then call submit_auth_code with the returned code."
        )

    expires_at = float(tokens.get("expires_at") or 0)
    if time.time() >= expires_at - EXPIRY_MARGIN_SECONDS:
        tokens = refresh_tokens(transport=transport)

    return str(tokens["access_token"])
