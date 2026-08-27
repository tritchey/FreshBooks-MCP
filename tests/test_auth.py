"""OAuth flow: persistence of the single-use refresh token, and failure safety."""

from __future__ import annotations

import json
import time

import httpx
import pytest

from freshbooks_mcp import auth, store


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    path = tmp_path / "state"
    monkeypatch.setenv(store.STATE_DIR_ENV, str(path))
    monkeypatch.setenv("FRESHBOOKS_CLIENT_ID", "cid")
    monkeypatch.setenv("FRESHBOOKS_CLIENT_SECRET", "secret")
    monkeypatch.setenv("FRESHBOOKS_REDIRECT_URI", "https://localhost:8414/callback")
    return path


def transport_returning(response: httpx.Response, seen: list[dict] | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/auth/oauth/token"
        if seen is not None:
            seen.append(json.loads(request.content))
        return response

    return httpx.MockTransport(handler)


def test_build_auth_url(state_dir):
    url = auth.build_auth_url()
    assert url.startswith("https://auth.freshbooks.com/oauth/authorize/?")
    assert "response_type=code" in url
    assert "client_id=cid" in url
    assert "redirect_uri=https%3A%2F%2Flocalhost%3A8414%2Fcallback" in url


def test_exchange_code_persists_tokens(state_dir):
    seen: list[dict] = []
    transport = transport_returning(
        httpx.Response(
            200,
            json={
                "access_token": "AT1",
                "refresh_token": "RT1",
                "expires_in": 3600,
                "created_at": 1_000_000,
            },
        ),
        seen,
    )

    auth.exchange_code("code-abc", transport=transport)

    assert seen[0] == {
        "grant_type": "authorization_code",
        "client_id": "cid",
        "client_secret": "secret",
        "redirect_uri": "https://localhost:8414/callback",
        "code": "code-abc",
    }
    tokens = store.load_tokens()
    assert tokens["access_token"] == "AT1"
    assert tokens["refresh_token"] == "RT1"
    assert tokens["expires_at"] == 1_000_000 + 3600


def test_exchange_code_without_created_at_uses_now(state_dir):
    transport = transport_returning(
        httpx.Response(
            200, json={"access_token": "AT1", "refresh_token": "RT1", "expires_in": 3600}
        )
    )

    before = time.time()
    auth.exchange_code("code-abc", transport=transport)

    assert before + 3600 <= store.load_tokens()["expires_at"] <= time.time() + 3600


def test_refresh_persists_the_new_refresh_token(state_dir):
    store.save_tokens(
        {"access_token": "AT1", "refresh_token": "RT1", "expires_at": 0, "business_id": 7}
    )
    seen: list[dict] = []
    transport = transport_returning(
        httpx.Response(
            200,
            json={
                "access_token": "AT2",
                "refresh_token": "RT2",
                "expires_in": 3600,
                "created_at": 2_000_000,
            },
        ),
        seen,
    )

    auth.refresh_tokens(transport=transport)

    assert seen[0]["grant_type"] == "refresh_token"
    assert seen[0]["refresh_token"] == "RT1"

    tokens = store.load_tokens()
    assert tokens["access_token"] == "AT2"
    assert tokens["refresh_token"] == "RT2"  # single-use: the old one is now dead
    assert tokens["expires_at"] == 2_000_000 + 3600
    assert tokens["business_id"] == 7  # unrelated fields survive


def test_failed_refresh_leaves_tokens_untouched(state_dir):
    store.save_tokens({"access_token": "AT1", "refresh_token": "RT1", "expires_at": 0})
    tokens_file = state_dir / store.TOKENS_FILE
    before = tokens_file.read_bytes()

    transport = transport_returning(
        httpx.Response(401, json={"error": "invalid_grant", "error_description": "token expired"})
    )

    with pytest.raises(auth.AuthError) as excinfo:
        auth.refresh_tokens(transport=transport)

    assert "invalid_grant" in str(excinfo.value)
    assert "401" in str(excinfo.value)
    assert tokens_file.read_bytes() == before


def test_ensure_access_token_returns_a_live_token_without_refreshing(state_dir):
    store.save_tokens(
        {"access_token": "AT1", "refresh_token": "RT1", "expires_at": time.time() + 3600}
    )

    def explode(request: httpx.Request) -> httpx.Response:
        raise AssertionError("should not have refreshed a live token")

    assert auth.ensure_access_token(transport=httpx.MockTransport(explode)) == "AT1"


def test_ensure_access_token_refreshes_near_expiry(state_dir):
    store.save_tokens(
        {"access_token": "AT1", "refresh_token": "RT1", "expires_at": time.time() + 30}
    )
    transport = transport_returning(
        httpx.Response(
            200, json={"access_token": "AT2", "refresh_token": "RT2", "expires_in": 3600}
        )
    )

    assert auth.ensure_access_token(transport=transport) == "AT2"
    assert store.load_tokens()["refresh_token"] == "RT2"


def test_ensure_access_token_without_tokens_explains_how_to_connect(state_dir):
    with pytest.raises(auth.AuthError, match="get_auth_url"):
        auth.ensure_access_token()
