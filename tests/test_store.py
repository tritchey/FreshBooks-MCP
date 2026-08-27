"""Local state: atomicity, permissions, and credential resolution."""

from __future__ import annotations

import json
import stat

import pytest

from freshbooks_mcp import store


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    path = tmp_path / "state"
    monkeypatch.setenv(store.STATE_DIR_ENV, str(path))
    for name in ("FRESHBOOKS_CLIENT_ID", "FRESHBOOKS_CLIENT_SECRET", "FRESHBOOKS_REDIRECT_URI"):
        monkeypatch.delenv(name, raising=False)
    return path


def test_state_dir_is_created_private(state_dir):
    created = store.state_dir()
    assert created.is_dir()
    assert stat.S_IMODE(created.stat().st_mode) == 0o700


def test_missing_files_read_as_empty(state_dir):
    assert store.load_tokens() == {}
    assert store.load_mapping() == {}
    assert store.load_ledger() == {}


def test_write_is_private_and_leaves_no_temp_files(state_dir):
    store.save_tokens({"access_token": "AT", "refresh_token": "RT", "expires_at": 1.5})

    tokens_file = state_dir / store.TOKENS_FILE
    assert stat.S_IMODE(tokens_file.stat().st_mode) == 0o600
    # os.replace() leaves the temp file behind only on failure.
    assert sorted(p.name for p in state_dir.iterdir()) == [store.TOKENS_FILE]


def test_roundtrip(state_dir):
    tokens = {"access_token": "AT", "refresh_token": "RT", "expires_at": 1.5, "business_id": 7}
    store.save_tokens(tokens)
    assert store.load_tokens() == tokens

    mapping = {"acme": {"project_id": 1, "client_id": 2, "project_title": "Acme"}}
    store.save_mapping(mapping)
    assert store.load_mapping() == mapping


def test_ledger_helpers(state_dir):
    key = store.ledger_key(123, "2026-08-24")
    assert key == "123:2026-08-24"

    store.set_ledger_entry(key, 5001)
    assert store.load_ledger() == {key: 5001}
    assert store.ledger_entry_ids() == {5001}

    assert store.drop_ledger_entry_id(9999) is False
    assert store.drop_ledger_entry_id(5001) is True
    assert store.load_ledger() == {}

    store.set_ledger_entry(key, 5002)
    store.drop_ledger_key(key)
    assert store.load_ledger() == {}


def test_credentials_from_env(state_dir, monkeypatch):
    monkeypatch.setenv("FRESHBOOKS_CLIENT_ID", "cid")
    monkeypatch.setenv("FRESHBOOKS_CLIENT_SECRET", "secret")

    creds = store.get_app_credentials()
    assert creds == {
        "client_id": "cid",
        "client_secret": "secret",
        "redirect_uri": store.DEFAULT_REDIRECT_URI,
    }


def test_credentials_fall_back_to_file(state_dir):
    store.state_dir()
    (state_dir / store.CREDENTIALS_FILE).write_text(
        json.dumps(
            {"client_id": "fid", "client_secret": "fsecret", "redirect_uri": "https://x/cb"}
        )
    )

    assert store.get_app_credentials() == {
        "client_id": "fid",
        "client_secret": "fsecret",
        "redirect_uri": "https://x/cb",
    }


def test_missing_credentials_name_what_is_missing(state_dir, monkeypatch):
    monkeypatch.setenv("FRESHBOOKS_CLIENT_ID", "cid")

    with pytest.raises(store.ConfigurationError) as excinfo:
        store.get_app_credentials()

    assert "FRESHBOOKS_CLIENT_SECRET" in str(excinfo.value)
    assert "FRESHBOOKS_CLIENT_ID" not in str(excinfo.value)
