"""FastMCP server exposing FreshBooks auth, lookups, and idempotent time and
expense logging."""

from __future__ import annotations

import functools
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Callable

from mcp.server.mcpserver import MCPServer

from . import __version__, auth, store
from .api import FreshBooksClient

# MCPServer is the mcp>=2 name for what used to be FastMCP; same high-level API.
mcp = MCPServer("freshbooks", version=__version__)

# Logged time has to land somewhere on the day; 09:00 local is a neutral choice
# that keeps entries on the intended calendar date in every timezone.
WORKDAY_START_HOUR = 9

# Expenses belong to a staff member. The account owner is always staff 1, which
# is what we fall back to when the identity carries no role for the account.
DEFAULT_STAFF_ID = 1

EXPENSE_STATUS = {0: "internal", 1: "outstanding", 2: "invoiced", 4: "recouped"}


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


def _require_account(tokens: dict[str, Any]) -> str:
    account_id = tokens.get("account_id")
    if not account_id:
        raise RuntimeError("No FreshBooks account_id stored. Re-run submit_auth_code.")
    return str(account_id)


def _staff_id_from_identity(identity: dict[str, Any], account_id: Any) -> int | None:
    """The Accounting service's staff id is the identity's `userid` on the account."""
    for role in identity.get("roles") or []:
        if str(role.get("accountid")) == str(account_id) and role.get("userid") is not None:
            return int(role["userid"])
    return None


def _resolve_staff_id(client: FreshBooksClient, tokens: dict[str, Any], account_id: str) -> int:
    """Staff id for new expenses: cached in tokens.json, else derived from the
    identity (and cached), else the account owner."""
    if tokens.get("staff_id") is not None:
        return int(tokens["staff_id"])
    staff_id = _staff_id_from_identity(client.get_identity(), account_id)
    if staff_id is None:
        return DEFAULT_STAFF_ID
    # Re-read before writing: the call above may have rotated the token pair.
    latest = store.load_tokens()
    latest["staff_id"] = staff_id
    store.save_tokens(latest)
    return staff_id


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


def _category_id(category: dict[str, Any]) -> int | None:
    value = category.get("id", category.get("categoryid"))
    return int(value) if value is not None else None


def _active_categories(categories: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [c for c in categories if not c.get("vis_state") and _category_id(c) is not None]


PATH_SEPARATOR = " > "


def _category_paths(categories: list[dict[str, Any]]) -> dict[int, str]:
    """{category_id: "Grandparent > Parent > Name"}. FreshBooks accounts carry
    several categories with the same bare name (a standard tree, a deprecated
    cost-of-goods-sold copy of it, and custom ones under "Other Expenses"), so
    the path is what tells them apart."""
    by_id = {_category_id(c): c for c in categories if _category_id(c) is not None}
    paths: dict[int, str] = {}
    for category_id, category in by_id.items():
        parts = [str(category.get("category") or "")]
        seen = {category_id}
        parent_id = category.get("parentid")
        while parent_id and int(parent_id) in by_id and int(parent_id) not in seen:
            seen.add(int(parent_id))
            parent = by_id[int(parent_id)]
            parts.append(str(parent.get("category") or ""))
            parent_id = parent.get("parentid")
        paths[category_id] = PATH_SEPARATOR.join(reversed(parts))  # type: ignore[index]
    return paths


def _normalize_path(text: str) -> str:
    return PATH_SEPARATOR.join(part.strip() for part in text.split(">")).lower()


def _format_amount(value: Any) -> str:
    """Positive decimal with two places, as the Accounting service's string-decimal."""
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        raise RuntimeError(f"Expense amount {value!r} is not a number.") from None
    if not amount.is_finite() or amount <= 0:
        raise RuntimeError(f"Expense amount must be positive, got {value!r}.")
    return str(amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _expense_summary(
    expense: dict[str, Any], category_paths: dict[int, str], refs: dict[int, str]
) -> dict[str, Any]:
    amount = expense.get("amount") or {}
    expense_id = expense.get("id")
    category_id = expense.get("categoryid")
    status = expense.get("status")
    owned = expense_id is not None and int(expense_id) in refs
    summary: dict[str, Any] = {
        "id": expense_id,
        "date": expense.get("date"),
        "amount": amount.get("amount"),
        "currency": amount.get("code"),
        "vendor": expense.get("vendor"),
        "notes": expense.get("notes"),
        "category_id": category_id,
        "category": category_paths.get(int(category_id)) if category_id is not None else None,
        "client_id": expense.get("clientid") or None,
        "project_id": expense.get("projectid") or None,
        "status": EXPENSE_STATUS.get(status, status),
        "has_receipt": expense.get("has_receipt"),
        "owned_by_ledger": owned,
    }
    if owned:
        summary["ref"] = refs[int(expense_id)]
    return summary


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
        staff_id = _staff_id_from_identity(identity, businesses[0]["account_id"])
        if staff_id is not None:
            tokens["staff_id"] = staff_id
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
        "staff_id": tokens.get("staff_id"),
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
    account_id = _require_account(tokens)

    with _make_client() as client:
        clients = client.list_clients(account_id)

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


# --------------------------------------------------------------------------
# expense tools
# --------------------------------------------------------------------------


@mcp.tool()
@_handle_errors
def list_expense_categories() -> dict[str, Any]:
    """List the FreshBooks expense categories for the connected account, as
    category_id, name, path ("Parent > Name"), parent_id and is_cogs (a
    deprecated cost-of-goods-sold duplicate of a regular category). Accounts
    usually hold several categories with the same bare name; log_expenses
    accepts a name when it is unambiguous (regular categories win over
    is_cogs ones), otherwise the full path or the category_id.
    """
    tokens = _require_connection()
    account_id = _require_account(tokens)
    with _make_client() as client:
        categories = _active_categories(client.list_expense_categories(account_id))

    paths = _category_paths(categories)
    return {
        "categories": sorted(
            (
                {
                    "category_id": _category_id(category),
                    "name": category.get("category"),
                    "path": paths[_category_id(category)],  # type: ignore[index]
                    "parent_id": category.get("parentid") or None,
                    "is_cogs": bool(category.get("is_cogs")),
                }
                for category in categories
            ),
            key=lambda item: (item["path"].lower(), item["is_cogs"]),
        )
    }


@mcp.tool()
@_handle_errors
def list_expenses(date_from: str, date_to: str) -> dict[str, Any]:
    """List expenses dated between two dates, inclusive (YYYY-MM-DD). Each
    expense reports id, date, amount, currency, vendor, notes, category_id,
    category (as a "Parent > Name" path), client_id, project_id, status
    (internal | outstanding | invoiced | recouped), has_receipt and owned_by_ledger - true when this server created
    the expense (its ref is included too) and may therefore update or delete it.
    Expenses with owned_by_ledger=false were entered by hand and are never
    modified. Expenses deleted in FreshBooks are omitted.
    """
    tokens = _require_connection()
    account_id = _require_account(tokens)
    date.fromisoformat(date_from)
    date.fromisoformat(date_to)

    with _make_client() as client:
        expenses = client.list_expenses(account_id, date_from=date_from, date_to=date_to)
        categories = client.list_expense_categories(account_id)

    paths = _category_paths(categories)
    refs = store.expense_ledger_refs()
    return {
        "expenses": [
            _expense_summary(expense, paths, refs)
            for expense in expenses
            if not expense.get("vis_state")
        ]
    }


def _resolve_category(entry: dict[str, Any], categories: list[dict[str, Any]]) -> int:
    """Resolve an input entry's category_id or category name to a category id."""
    if entry.get("category_id") is not None:
        category_id = int(entry["category_id"])
        if not any(_category_id(c) == category_id for c in categories):
            raise RuntimeError(
                f"No expense category with id {category_id}. "
                "Call list_expense_categories to see the available ids."
            )
        return category_id

    name = str(entry.get("category") or "").strip()
    if not name:
        raise RuntimeError("Entry needs either a 'category' name or a numeric 'category_id'.")

    paths = _category_paths(categories)
    wanted = _normalize_path(name)
    matches = [
        c
        for c in categories
        if str(c.get("category") or "").strip().lower() == wanted
        or paths[_category_id(c)].lower() == wanted  # type: ignore[index]
    ]
    if not matches:
        known = ", ".join(sorted({str(c.get("category")) for c in categories})) or "(none)"
        raise RuntimeError(f"Unknown expense category {name!r}. Known categories: {known}.")
    if len(matches) > 1:
        # The cost-of-goods-sold tree duplicates the regular one name for name
        # and is deprecated; only pick it when it is the sole match.
        regular = [c for c in matches if not c.get("is_cogs")]
        if len(regular) == 1:
            matches = regular
    if len(matches) > 1:
        options = "; ".join(
            f"{paths[_category_id(c)]!r} (id {_category_id(c)}"  # type: ignore[index]
            + (", cost of goods sold)" if c.get("is_cogs") else ")")
            for c in matches
        )
        raise RuntimeError(
            f"Expense category {name!r} is ambiguous: {options}. "
            "Pass the full path (for example 'Other Expenses > Travel') or the category_id."
        )
    return int(_category_id(matches[0]))  # type: ignore[arg-type]


def _expense_matches(current: dict[str, Any], payload: dict[str, Any]) -> bool:
    """True when the expense FreshBooks holds already equals what we would write."""
    current_amount = current.get("amount") or {}
    try:
        if Decimal(str(current_amount.get("amount"))) != Decimal(payload["amount"]["amount"]):
            return False
        if "markup_percent" in payload and Decimal(
            str(current.get("markup_percent") or 0)
        ) != Decimal(payload["markup_percent"]):
            return False
    except InvalidOperation:
        return False

    wanted_code = payload["amount"].get("code")
    if wanted_code and str(current_amount.get("code") or "").upper() != wanted_code:
        return False

    return (
        int(current.get("categoryid") or 0) == payload["categoryid"]
        and str(current.get("date") or "") == payload["date"]
        and (current.get("vendor") or "") == payload["vendor"]
        and (current.get("notes") or "") == payload["notes"]
        and int(current.get("clientid") or 0) == payload["clientid"]
        and int(current.get("projectid") or 0) == payload["projectid"]
    )


@mcp.tool()
@_handle_errors
def log_expenses(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Log expenses to FreshBooks idempotently. Each entry is a dict with:
    date ("YYYY-MM-DD"), amount (number; two decimals are kept), and either
    category (a name or "Parent > Name" path from list_expense_categories,
    case-insensitive; the path is needed when the bare name is ambiguous) or
    category_id (int). Optional: vendor, notes, currency (3-letter code, else
    the business currency), markup_percent, and - to bill the expense to a
    client - either label / project_id (as in log_time; the project's client is
    used) or a bare client_id. Without a client the expense is internal.

    ref (optional) is a short stable identifier of your choosing, such as a
    receipt number or "2026-09-01-uber". Re-running with the same ref updates
    the expense this server previously created; if nothing changed it is
    reported as "unchanged" and no write is made. When ref is omitted it is
    derived from date, vendor and amount, so an identical re-run is still a
    no-op but a changed amount creates a new expense - pass a ref whenever you
    may need to correct an expense later. Expenses created outside this server
    are never modified or deleted.

    Returns per-entry results with action created | updated | unchanged | failed
    (failures are per entry; the rest of the batch still runs) plus a summary.
    """
    tokens = _require_connection()
    business_id = int(tokens["business_id"])
    account_id = _require_account(tokens)
    mapping = store.load_mapping()

    results: list[dict[str, Any]] = []
    summary = {"created": 0, "updated": 0, "unchanged": 0, "failed": 0}

    with _make_client() as client:
        project_cache: dict[int, dict[str, Any]] | None = None
        category_cache: list[dict[str, Any]] | None = None
        staff_cache: int | None = None

        def lookup_project(project_id: int) -> dict[str, Any] | None:
            nonlocal project_cache
            if project_cache is None:
                project_cache = {
                    int(p["id"]): p
                    for p in client.list_projects(business_id, active_only=False)
                    if p.get("id") is not None
                }
            return project_cache.get(project_id)

        def categories() -> list[dict[str, Any]]:
            nonlocal category_cache
            if category_cache is None:
                category_cache = _active_categories(client.list_expense_categories(account_id))
            return category_cache

        def staff_id() -> int:
            nonlocal staff_cache
            if staff_cache is None:
                staff_cache = _resolve_staff_id(client, tokens, account_id)
            return staff_cache

        for raw in entries:
            try:
                result = _log_one_expense(
                    client, account_id, mapping, lookup_project, categories, staff_id, raw
                )
            except Exception as exc:  # noqa: BLE001 - one bad entry must not sink the batch
                result = {
                    "ref": raw.get("ref"),
                    "date": raw.get("date"),
                    "amount": raw.get("amount"),
                    "action": "failed",
                    "error": str(exc) or exc.__class__.__name__,
                }
            summary[result["action"]] += 1
            results.append(result)

    return {"results": results, "summary": summary}


def _log_one_expense(
    client: FreshBooksClient,
    account_id: str,
    mapping: dict[str, Any],
    lookup_project: Callable[[int], dict[str, Any] | None],
    categories: Callable[[], list[dict[str, Any]]],
    staff_id: Callable[[], int],
    raw: dict[str, Any],
) -> dict[str, Any]:
    day = str(raw.get("date") or "")
    if not day:
        raise RuntimeError("Entry is missing 'date' (YYYY-MM-DD).")
    date.fromisoformat(day)  # validate the format up front

    if raw.get("amount") is None:
        raise RuntimeError("Entry is missing 'amount'.")
    amount = _format_amount(raw["amount"])
    currency = str(raw["currency"]).strip().upper() if raw.get("currency") else None
    vendor = str(raw.get("vendor") or "")
    notes = str(raw.get("notes") or "")
    category_id = _resolve_category(raw, categories())

    project_id, client_id = 0, 0
    if raw.get("label") or raw.get("project_id") is not None:
        project_id, project_client = _resolve_target(raw, mapping, lookup_project)
        client_id = int(project_client or raw.get("client_id") or 0)
    elif raw.get("client_id") is not None:
        client_id = int(raw["client_id"])

    ref = str(raw.get("ref") or "").strip() or f"{day}|{vendor}|{amount}"

    payload: dict[str, Any] = {
        "amount": {"amount": amount, **({"code": currency} if currency else {})},
        "categoryid": category_id,
        "date": day,
        "vendor": vendor,
        "notes": notes,
        "clientid": client_id,
        "projectid": project_id,
    }
    if raw.get("markup_percent") is not None:
        payload["markup_percent"] = str(raw["markup_percent"])

    def outcome(action: str, expense_id: int) -> dict[str, Any]:
        return {
            "ref": ref,
            "date": day,
            "amount": amount,
            "action": action,
            "expense_id": expense_id,
        }

    existing_id = store.load_expense_ledger().get(ref)
    if existing_id is not None:
        current = client.get_expense(account_id, int(existing_id))
        if current is None or current.get("vis_state") == 1:
            # Deleted in FreshBooks since we wrote it; forget it and start over.
            store.drop_expense_ledger_key(ref)
            existing_id = None
        elif _expense_matches(current, payload):
            return outcome("unchanged", int(existing_id))
        else:
            client.update_expense(account_id, int(existing_id), payload)
            return outcome("updated", int(existing_id))

    owner = int(raw["staff_id"]) if raw.get("staff_id") is not None else staff_id()
    created = client.create_expense(account_id, {**payload, "staffid": owner})
    new_id = created.get("id")
    if new_id is None:
        raise RuntimeError(f"FreshBooks did not return an id for the created expense: {created}")
    store.set_expense_ledger_entry(ref, int(new_id))
    return outcome("created", int(new_id))


@mcp.tool()
@_handle_errors
def delete_expense(expense_id: int) -> dict[str, Any]:
    """Delete an expense that this server created. Refuses any id that is not in
    the local expense ledger, so hand-entered FreshBooks expenses can never be
    deleted through this tool. Use list_expenses to see which expenses are
    owned_by_ledger.
    """
    tokens = _require_connection()
    account_id = _require_account(tokens)
    if int(expense_id) not in store.expense_ledger_ids():
        raise RuntimeError(
            f"Refusing to delete expense {expense_id}: it was not created by this "
            "server (not in the local ledger). Delete it in FreshBooks if that is intended."
        )

    with _make_client() as client:
        client.delete_expense(account_id, int(expense_id))
    store.drop_expense_ledger_id(int(expense_id))
    return {"deleted": True, "expense_id": int(expense_id)}


def main() -> None:
    """Run the MCP server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
