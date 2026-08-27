"""FastMCP server exposing FreshBooks auth, lookups, and idempotent time logging."""

from __future__ import annotations

import functools
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

from mcp.server.mcpserver import MCPServer

from . import __version__, auth, store
from .api import FreshBooksClient

# MCPServer is the mcp>=2 name for what used to be FastMCP; same high-level API.
mcp = MCPServer("freshbooks", version=__version__)

# Logged time has to land somewhere on the day; 09:00 local is a neutral choice
# that keeps entries on the intended calendar date in every timezone.
WORKDAY_START_HOUR = 9


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _make_client() -> FreshBooksClient:
    """Client factory; tests replace this to inject an httpx.MockTransport."""
    return FreshBooksClient()


def _handle_errors(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Turn auth/HTTP failures into a readable tool result instead of a traceback."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - the tool result is the error channel
            return {"error": str(exc) or exc.__class__.__name__}

    return wrapper


def _require_connection() -> dict[str, Any]:
    tokens = store.load_tokens()
    if not tokens.get("access_token"):
        raise RuntimeError(
            "Not connected to FreshBooks. Call get_auth_url, approve access, "
            "then call submit_auth_code."
        )
    if not tokens.get("business_id"):
        raise RuntimeError(
            "No FreshBooks business selected. Re-run submit_auth_code to store the business id."
        )
    return tokens


def _utc_iso(moment: datetime) -> str:
    """ISO-8601 UTC with milliseconds and a Z suffix, as FreshBooks expects."""
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _local_midnight(day: str) -> datetime:
    """Local midnight on `day`. A naive datetime's .astimezone() localizes it,
    picking up the correct (DST-aware) system offset for that date."""
    parsed = date.fromisoformat(day)
    return datetime(parsed.year, parsed.month, parsed.day).astimezone()


def _started_at(day: str) -> str:
    parsed = date.fromisoformat(day)
    return _utc_iso(datetime(parsed.year, parsed.month, parsed.day, WORKDAY_START_HOUR).astimezone())


def _local_day_of(started_at: str) -> str:
    moment = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone().date().isoformat()


def _client_name(client: dict[str, Any]) -> str:
    organization = (client.get("organization") or "").strip()
    if organization:
        return organization
    return " ".join(part for part in (client.get("fname"), client.get("lname")) if part).strip()


def _project_summary(project: dict[str, Any]) -> dict[str, Any]:
    return {
        "project_id": project.get("id"),
        "title": project.get("title"),
        "client_id": project.get("client_id"),
        "project_type": project.get("project_type"),
        "rate": project.get("rate"),
        "active": project.get("active"),
        "complete": project.get("complete"),
    }


# --------------------------------------------------------------------------
# auth tools
# --------------------------------------------------------------------------


@mcp.tool()
@_handle_errors
def get_auth_url() -> dict[str, Any]:
    """Step 1 of connecting to FreshBooks. Returns the OAuth consent URL to open
    in a browser. After approving, the browser is redirected to a localhost URL
    that will NOT load - that is expected. Copy the value of the `code=` query
    parameter out of the address bar and pass it to submit_auth_code.

    Requires the FreshBooks app credentials to be configured via the
    FRESHBOOKS_CLIENT_ID / FRESHBOOKS_CLIENT_SECRET environment variables or
    ~/.freshbooks-mcp/credentials.json.
    """
    return {
        "auth_url": auth.build_auth_url(),
        "instructions": [
            "Open auth_url in a browser and sign in to FreshBooks.",
            "Approve access for the application.",
            "The browser lands on the redirect URI, which will fail to load - that is normal.",
            "Copy the value of the `code=` query parameter from the address bar.",
            "Call submit_auth_code with that code. Codes are short-lived, so do it promptly.",
        ],
    }


@mcp.tool()
@_handle_errors
def submit_auth_code(code: str) -> dict[str, Any]:
    """Step 2 of connecting to FreshBooks. Exchanges the authorization code from
    get_auth_url for tokens, stores them, and records the identity plus the
    business to work against. Returns who is connected and every business the
    account belongs to.
    """
    auth.exchange_code(code)

    with _make_client() as client:
        identity = client.get_identity()

    businesses = [
        {
            "business_id": (membership.get("business") or {}).get("id"),
            "name": (membership.get("business") or {}).get("name"),
            "account_id": (membership.get("business") or {}).get("account_id"),
            "role": membership.get("role"),
        }
        for membership in (identity.get("business_memberships") or [])
        if membership.get("business")
    ]

    tokens = store.load_tokens()
    tokens["identity_id"] = identity.get("id")
    if businesses:
        tokens["business_id"] = businesses[0]["business_id"]
        tokens["account_id"] = businesses[0]["account_id"]
    store.save_tokens(tokens)

    name = " ".join(
        part for part in (identity.get("first_name"), identity.get("last_name")) if part
    ).strip()

    result: dict[str, Any] = {
        "connected_as": name,
        "email": identity.get("email"),
        "identity_id": identity.get("id"),
        "businesses": businesses,
        "active_business": businesses[0] if businesses else None,
    }
    if not businesses:
        result["warning"] = "This FreshBooks identity has no business memberships."
    elif len(businesses) > 1:
        result["note"] = (
            "Multiple businesses found; the first one is active. "
            "Choosing a different business is not implemented yet."
        )
    return result


@mcp.tool()
@_handle_errors
def whoami() -> dict[str, Any]:
    """Auth health check. Makes a live call to FreshBooks with the stored token
    (refreshing it if needed) and returns the connected identity along with the
    business_id and account_id in use. Use this to confirm the connection works
    before logging time.
    """
    tokens = store.load_tokens()
    if not tokens.get("access_token"):
        raise RuntimeError("Not connected to FreshBooks. Call get_auth_url to start.")

    with _make_client() as client:
        identity = client.get_identity()

    name = " ".join(
        part for part in (identity.get("first_name"), identity.get("last_name")) if part
    ).strip()
    return {
        "connected": True,
        "connected_as": name,
        "email": identity.get("email"),
        "identity_id": identity.get("id"),
        "business_id": tokens.get("business_id"),
        "account_id": tokens.get("account_id"),
        "token_expires_at": tokens.get("expires_at"),
    }


# --------------------------------------------------------------------------
# lookup tools
# --------------------------------------------------------------------------


@mcp.tool()
@_handle_errors
def list_projects(active_only: bool = True) -> dict[str, Any]:
    """List FreshBooks projects for the connected business. Each project has
    project_id, title, client_id, project_type, rate, active and complete.
    Set active_only=False to include finished and archived projects.
    """
    tokens = _require_connection()
    with _make_client() as client:
        projects = client.list_projects(int(tokens["business_id"]), active_only=active_only)
    return {"projects": [_project_summary(p) for p in projects]}


@mcp.tool()
@_handle_errors
def list_clients() -> dict[str, Any]:
    """List FreshBooks clients for the connected account, as client_id,
    organization, name and email.
    """
    tokens = _require_connection()
    account_id = tokens.get("account_id")
    if not account_id:
        raise RuntimeError("No FreshBooks account_id stored. Re-run submit_auth_code.")

    with _make_client() as client:
        clients = client.list_clients(str(account_id))

    return {
        "clients": [
            {
                "client_id": entry.get("id"),
                "organization": entry.get("organization"),
                "name": _client_name(entry),
                "email": entry.get("email"),
            }
            for entry in clients
        ]
    }


@mcp.tool()
@_handle_errors
def get_mapping() -> dict[str, Any]:
    """Return the saved label -> project mapping. Labels are the short names
    log_time accepts instead of a numeric project_id.
    """
    mapping = store.load_mapping()
    return {
        "mapping": mapping,
        "labels": sorted(mapping),
        "hint": (
            "To map a new label: call list_projects to find the project_id, then "
            "set_mapping(label, project_id). log_time accepts either a mapped label "
            "or a raw project_id; unmapped labels fail that entry and are reported "
            "individually."
        ),
    }


@mcp.tool()
@_handle_errors
def set_mapping(label: str, project_id: int) -> dict[str, Any]:
    """Map a short label to a FreshBooks project so log_time can use the label.
    The project must exist (active or not); its client_id and title are resolved
    and stored alongside the id.
    """
    tokens = _require_connection()
    with _make_client() as client:
        projects = client.list_projects(int(tokens["business_id"]), active_only=False)

    match = next((p for p in projects if int(p.get("id", -1)) == int(project_id)), None)
    if match is None:
        raise RuntimeError(
            f"No project with id {project_id} in this business. "
            "Call list_projects(active_only=False) to see the available ids."
        )

    entry = {
        "project_id": int(match["id"]),
        "client_id": match.get("client_id"),
        "project_title": match.get("title"),
    }
    store.set_mapping_entry(label, entry)
    return {"label": label, **entry, "mapping": store.load_mapping()}


# --------------------------------------------------------------------------
# time tools
# --------------------------------------------------------------------------


@mcp.tool()
@_handle_errors
def list_time_entries(started_from: str, started_to: str) -> dict[str, Any]:
    """List time entries between two dates, inclusive. Dates are YYYY-MM-DD in
    local time. Each entry reports id, date (local), minutes, project_id,
    client_id, note, billable, billed, and owned_by_ledger - true when this
    server created the entry and may therefore update or delete it. Entries with
    owned_by_ledger=false were entered by hand and are never modified.
    """
    tokens = _require_connection()
    # started_to is inclusive, so the API window ends at the start of the next day.
    window_start = _utc_iso(_local_midnight(started_from))
    window_end = _utc_iso(_local_midnight(started_to) + timedelta(days=1))

    with _make_client() as client:
        entries = client.list_time_entries(
            int(tokens["business_id"]), started_from=window_start, started_to=window_end
        )

    owned = store.ledger_entry_ids()
    return {
        "time_entries": [
            {
                "id": entry.get("id"),
                "date": _local_day_of(entry.get("started_at") or ""),
                "minutes": round((entry.get("duration") or 0) / 60, 2),
                "project_id": entry.get("project_id"),
                "client_id": entry.get("client_id"),
                "note": entry.get("note"),
                "billable": entry.get("billable"),
                "billed": entry.get("billed"),
                "owned_by_ledger": int(entry.get("id", -1)) in owned,
            }
            for entry in entries
        ]
    }


def _resolve_target(
    entry: dict[str, Any],
    mapping: dict[str, Any],
    lookup_project: Callable[[int], dict[str, Any] | None],
) -> tuple[int, Any]:
    """Resolve an input entry to (project_id, client_id) via label or project_id."""
    label = entry.get("label")
    if label:
        mapped = mapping.get(label)
        if not mapped:
            known = ", ".join(sorted(mapping)) or "(none)"
            raise RuntimeError(
                f"Unknown label {label!r}. Known labels: {known}. "
                "Use set_mapping(label, project_id) to add it."
            )
        return int(mapped["project_id"]), mapped.get("client_id")

    if entry.get("project_id") is not None:
        project_id = int(entry["project_id"])
        project = lookup_project(project_id)
        if project is None:
            raise RuntimeError(
                f"No project with id {project_id} in this business. "
                "Call list_projects(active_only=False) to see the available ids."
            )
        return project_id, project.get("client_id")

    raise RuntimeError("Entry needs either a mapped 'label' or a numeric 'project_id'.")


@mcp.tool()
@_handle_errors
def log_time(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Log time to FreshBooks idempotently. Each entry is a dict with:
    date ("YYYY-MM-DD"), minutes (number), note (string), either label (a mapped
    label from get_mapping) or project_id (int), and optional billable (default
    true).

    One entry is kept per project per day. Re-running with the same date and
    project updates the entry this server previously created; if nothing changed
    the entry is reported as "unchanged" and no write is made. Entries created
    outside this server are never modified or deleted, even on the same project
    and day. Time is recorded starting at 09:00 local on the given date.

    Returns per-entry results with action created | updated | unchanged | failed
    (failures are per entry; the rest of the batch still runs) plus a summary.
    """
    tokens = _require_connection()
    business_id = int(tokens["business_id"])
    identity_id = tokens.get("identity_id")
    mapping = store.load_mapping()

    results: list[dict[str, Any]] = []
    summary = {"created": 0, "updated": 0, "unchanged": 0, "failed": 0}

    with _make_client() as client:
        cache: dict[int, dict[str, Any]] | None = None

        def lookup_project(project_id: int) -> dict[str, Any] | None:
            nonlocal cache
            if cache is None:
                cache = {
                    int(p["id"]): p
                    for p in client.list_projects(business_id, active_only=False)
                    if p.get("id") is not None
                }
            return cache.get(project_id)

        for raw in entries:
            try:
                result = _log_one(client, business_id, identity_id, mapping, lookup_project, raw)
            except Exception as exc:  # noqa: BLE001 - one bad entry must not sink the batch
                result = {
                    "date": raw.get("date"),
                    "project_id": raw.get("project_id"),
                    "action": "failed",
                    "minutes": raw.get("minutes"),
                    "error": str(exc) or exc.__class__.__name__,
                }
            summary[result["action"]] += 1
            results.append(result)

    return {"results": results, "summary": summary}


def _log_one(
    client: FreshBooksClient,
    business_id: int,
    identity_id: Any,
    mapping: dict[str, Any],
    lookup_project: Callable[[int], dict[str, Any] | None],
    raw: dict[str, Any],
) -> dict[str, Any]:
    day = str(raw.get("date") or "")
    if not day:
        raise RuntimeError("Entry is missing 'date' (YYYY-MM-DD).")
    date.fromisoformat(day)  # validate the format up front

    if raw.get("minutes") is None:
        raise RuntimeError("Entry is missing 'minutes'.")
    minutes = float(raw["minutes"])
    if minutes <= 0:
        raise RuntimeError(f"Entry minutes must be positive, got {raw['minutes']!r}.")
    duration = int(round(minutes * 60))

    note = str(raw.get("note") or "")
    billable = bool(raw.get("billable", True))
    project_id, client_id = _resolve_target(raw, mapping, lookup_project)
    started_at = _started_at(day)

    key = store.ledger_key(project_id, day)
    existing_id = store.load_ledger().get(key)

    if existing_id is not None:
        current = client.get_time_entry(business_id, int(existing_id))
        if current is None:
            # Deleted in FreshBooks since we wrote it; forget it and start over.
            store.drop_ledger_key(key)
            existing_id = None
        elif int(current.get("duration") or 0) == duration and (current.get("note") or "") == note:
            return {
                "date": day,
                "project_id": project_id,
                "action": "unchanged",
                "time_entry_id": int(existing_id),
                "minutes": minutes,
            }
        else:
            client.update_time_entry(
                business_id,
                int(existing_id),
                {
                    "duration": duration,
                    "note": note,
                    "started_at": started_at,
                    "is_logged": True,
                    "billable": billable,
                    "client_id": client_id,
                    "project_id": project_id,
                },
            )
            return {
                "date": day,
                "project_id": project_id,
                "action": "updated",
                "time_entry_id": int(existing_id),
                "minutes": minutes,
            }

    created = client.create_time_entry(
        business_id,
        {
            "is_logged": True,
            "duration": duration,
            "note": note,
            "started_at": started_at,
            "client_id": client_id,
            "project_id": project_id,
            "identity_id": identity_id,
            "billable": billable,
        },
    )
    new_id = created.get("id")
    if new_id is None:
        raise RuntimeError(f"FreshBooks did not return an id for the created entry: {created}")
    store.set_ledger_entry(key, int(new_id))
    return {
        "date": day,
        "project_id": project_id,
        "action": "created",
        "time_entry_id": int(new_id),
        "minutes": minutes,
    }


@mcp.tool()
@_handle_errors
def delete_time_entry(time_entry_id: int) -> dict[str, Any]:
    """Delete a time entry that this server created. Refuses any id that is not
    in the local ledger, so hand-entered FreshBooks time can never be deleted
    through this tool. Use list_time_entries to see which entries are
    owned_by_ledger.
    """
    tokens = _require_connection()
    if int(time_entry_id) not in store.ledger_entry_ids():
        raise RuntimeError(
            f"Refusing to delete time entry {time_entry_id}: it was not created by this "
            "server (not in the local ledger). Delete it in FreshBooks if that is intended."
        )

    with _make_client() as client:
        client.delete_time_entry(int(tokens["business_id"]), int(time_entry_id))
    store.drop_ledger_entry_id(int(time_entry_id))
    return {"deleted": True, "time_entry_id": int(time_entry_id)}


def main() -> None:
    """Run the MCP server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
