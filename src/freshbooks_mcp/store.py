"""Local state: OAuth tokens, label->project mapping, and the write ledgers.

Two ledgers record what this server created so re-syncs update instead of
duplicating, and so nothing entered by hand in FreshBooks is ever touched:
ledger.json maps "<project_id>:<date>" to a time entry id, expense_ledger.json
maps a caller-chosen ref to an expense id.

Every write goes through _write_json, which writes a temp file in the same
directory and os.replace()s it into position. FreshBooks refresh tokens are
single-use -- a torn tokens.json means the connection can never be refreshed
again and the user has to re-authorize by hand.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

STATE_DIR_ENV = "FRESHBOOKS_MCP_STATE_DIR"
DEFAULT_STATE_DIR = Path.home() / ".freshbooks-mcp"

TOKENS_FILE = "tokens.json"
MAPPING_FILE = "mapping.json"
LEDGER_FILE = "ledger.json"
EXPENSE_LEDGER_FILE = "expense_ledger.json"
CREDENTIALS_FILE = "credentials.json"

DEFAULT_REDIRECT_URI = "https://localhost:8414/callback"


# --------------------------------------------------------------------------
# paths / raw io
# --------------------------------------------------------------------------


def state_dir() -> Path:
    """Return the state directory, creating it 0700 on first use."""
    override = os.environ.get(STATE_DIR_ENV)
    path = Path(override).expanduser() if override else DEFAULT_STATE_DIR
    if not path.exists():
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(path, 0o700)  # mkdir's mode is masked by umask; be explicit
    return path


def _path(name: str) -> Path:
    return state_dir() / name


def _read_json(name: str) -> Any | None:
    try:
        with _path(name).open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return None


def _write_json(name: str, data: Any) -> None:
    """Atomically write `data` as JSON to <state_dir>/<name> with mode 0600."""
    directory = state_dir()
    fd, tmp_name = tempfile.mkstemp(dir=directory, prefix=f".{name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, directory / name)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


# --------------------------------------------------------------------------
# tokens.json
# --------------------------------------------------------------------------


def load_tokens() -> dict[str, Any]:
    """{access_token, refresh_token, expires_at, business_id, account_id, identity_id, staff_id}."""
    return _read_json(TOKENS_FILE) or {}


def save_tokens(tokens: dict[str, Any]) -> None:
    _write_json(TOKENS_FILE, tokens)


# --------------------------------------------------------------------------
# mapping.json
# --------------------------------------------------------------------------


def load_mapping() -> dict[str, Any]:
    """{label: {project_id, client_id, project_title}}."""
    return _read_json(MAPPING_FILE) or {}


def save_mapping(mapping: dict[str, Any]) -> None:
    _write_json(MAPPING_FILE, mapping)


def set_mapping_entry(label: str, entry: dict[str, Any]) -> dict[str, Any]:
    mapping = load_mapping()
    mapping[label] = entry
    save_mapping(mapping)
    return mapping


# --------------------------------------------------------------------------
# ledgers: ledger.json (time entries) and expense_ledger.json (expenses)
# --------------------------------------------------------------------------


def _load_ledger(name: str) -> dict[str, int]:
    return _read_json(name) or {}


def _set_ledger_entry(name: str, key: str, remote_id: int) -> None:
    ledger = _load_ledger(name)
    ledger[key] = int(remote_id)
    _write_json(name, ledger)


def _drop_ledger_key(name: str, key: str) -> None:
    ledger = _load_ledger(name)
    if ledger.pop(key, None) is not None:
        _write_json(name, ledger)


def _drop_ledger_id(name: str, remote_id: int) -> bool:
    """Remove whatever rows point at `remote_id`. True if any were removed."""
    ledger = _load_ledger(name)
    keys = [k for k, v in ledger.items() if int(v) == int(remote_id)]
    for key in keys:
        del ledger[key]
    if keys:
        _write_json(name, ledger)
    return bool(keys)


def ledger_key(project_id: int, date: str) -> str:
    return f"{project_id}:{date}"


def load_ledger() -> dict[str, int]:
    """{"<project_id>:<YYYY-MM-DD>": time_entry_id}."""
    return _load_ledger(LEDGER_FILE)


def save_ledger(ledger: dict[str, int]) -> None:
    _write_json(LEDGER_FILE, ledger)


def set_ledger_entry(key: str, time_entry_id: int) -> None:
    _set_ledger_entry(LEDGER_FILE, key, time_entry_id)


def drop_ledger_key(key: str) -> None:
    _drop_ledger_key(LEDGER_FILE, key)


def drop_ledger_entry_id(time_entry_id: int) -> bool:
    """Remove whatever ledger row points at `time_entry_id`. True if one was removed."""
    return _drop_ledger_id(LEDGER_FILE, time_entry_id)


def ledger_entry_ids() -> set[int]:
    return {int(v) for v in load_ledger().values()}


def load_expense_ledger() -> dict[str, int]:
    """{"<ref>": expense_id}, where ref is the caller's idempotency key."""
    return _load_ledger(EXPENSE_LEDGER_FILE)


def save_expense_ledger(ledger: dict[str, int]) -> None:
    _write_json(EXPENSE_LEDGER_FILE, ledger)


def set_expense_ledger_entry(ref: str, expense_id: int) -> None:
    _set_ledger_entry(EXPENSE_LEDGER_FILE, ref, expense_id)


def drop_expense_ledger_key(ref: str) -> None:
    _drop_ledger_key(EXPENSE_LEDGER_FILE, ref)


def drop_expense_ledger_id(expense_id: int) -> bool:
    """Remove whatever ledger row points at `expense_id`. True if one was removed."""
    return _drop_ledger_id(EXPENSE_LEDGER_FILE, expense_id)


def expense_ledger_ids() -> set[int]:
    return {int(v) for v in load_expense_ledger().values()}


def expense_ledger_refs() -> dict[int, str]:
    """Inverse of the expense ledger: {expense_id: ref}."""
    return {int(v): k for k, v in load_expense_ledger().items()}


# --------------------------------------------------------------------------
# app credentials
# --------------------------------------------------------------------------


class ConfigurationError(RuntimeError):
    """Raised when the FreshBooks OAuth app credentials are not configured."""


def get_app_credentials() -> dict[str, str]:
    """Return {client_id, client_secret, redirect_uri} from env or credentials.json."""
    file_creds = _read_json(CREDENTIALS_FILE) or {}

    client_id = os.environ.get("FRESHBOOKS_CLIENT_ID") or file_creds.get("client_id")
    client_secret = os.environ.get("FRESHBOOKS_CLIENT_SECRET") or file_creds.get("client_secret")
    redirect_uri = (
        os.environ.get("FRESHBOOKS_REDIRECT_URI")
        or file_creds.get("redirect_uri")
        or DEFAULT_REDIRECT_URI
    )

    missing = [
        name
        for name, value in (
            ("FRESHBOOKS_CLIENT_ID (or credentials.json 'client_id')", client_id),
            ("FRESHBOOKS_CLIENT_SECRET (or credentials.json 'client_secret')", client_secret),
        )
        if not value
    ]
    if missing:
        raise ConfigurationError(
            "FreshBooks app credentials are not configured. Missing: "
            + ", ".join(missing)
            + f". Set the environment variables, or create {state_dir() / CREDENTIALS_FILE} "
            'containing {"client_id": "...", "client_secret": "...", "redirect_uri": "..."}. '
            "Create the app at https://my.freshbooks.com/#/developer."
        )

    return {
        "client_id": str(client_id),
        "client_secret": str(client_secret),
        "redirect_uri": str(redirect_uri),
    }
