"""Thin synchronous HTTP client for the FreshBooks API."""

from __future__ import annotations

from typing import Any

import httpx

from . import auth

BASE_URL = "https://api.freshbooks.com"
PER_PAGE = 100
MAX_PAGES = 200  # guard against a server that never stops advertising more pages

# FreshBooks' docs are explicit that GETs to the Projects and Time Tracking
# services must NOT carry a Content-Type header; they reject the request if it
# is present.
_NO_CONTENT_TYPE_ON_GET = ("/projects/", "/timetracking/")

# FreshBooks binds scopes to the app registration, not the token request, and a
# token only carries the scopes the app had when it was issued.
SCOPE_HINT = (
    "The stored token was issued without a scope this call needs. Add the missing "
    "scope(s) to the app at https://my.freshbooks.com/#/developer, then reconnect with "
    "get_auth_url and submit_auth_code so a new token is issued with them."
)


class FreshBooksError(RuntimeError):
    """Raised for any non-2xx response from FreshBooks."""


class FreshBooksClient:
    """FreshBooks REST client that keeps its own OAuth access token fresh."""

    def __init__(
        self,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
        base_url: str = BASE_URL,
    ) -> None:
        self._transport = transport
        self._client = httpx.Client(base_url=base_url, timeout=timeout, transport=transport)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> FreshBooksClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # transport
    # ------------------------------------------------------------------

    def _headers(self, method: str, path: str, token: str) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        if method.upper() != "GET" or not path.startswith(_NO_CONTENT_TYPE_ON_GET):
            headers["Content-Type"] = "application/json"
        if path.startswith("/accounting/"):
            headers["Api-Version"] = "alpha"
        return headers

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        missing_ok: bool = False,
    ) -> Any:
        token = auth.ensure_access_token(transport=self._transport)
        response = self._client.request(
            method,
            path,
            params=params,
            json=json_body,
            headers=self._headers(method, path, token),
        )

        if response.status_code == 401:
            # The access token went stale early (revoked, clock skew, rotated
            # elsewhere). Refresh once and replay the request exactly once.
            token = auth.refresh_tokens(transport=self._transport)["access_token"]
            response = self._client.request(
                method,
                path,
                params=params,
                json=json_body,
                headers=self._headers(method, path, token),
            )

        if missing_ok and response.status_code == 404:
            return None

        if response.status_code < 200 or response.status_code >= 300:
            message = (
                f"FreshBooks {method.upper()} {path} failed "
                f"(HTTP {response.status_code}): {response.text}"
            )
            if "insufficient_scope" in response.text:
                message = f"{message} {SCOPE_HINT}"
            raise FreshBooksError(message)

        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError:
            return {}

    def _paginate(self, path: str, key: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Collect `key` across pages of a Projects / Time Tracking style response."""
        items: list[dict[str, Any]] = []
        page = 1
        while page <= MAX_PAGES:
            query = dict(params or {})
            query.update({"per_page": PER_PAGE, "page": page})
            data = self._request("GET", path, params=query) or {}
            items.extend(data.get(key) or [])
            meta = data.get("meta") or {}
            pages = int(meta.get("pages") or 1)
            if page >= pages:
                break
            page += 1
        return items

    def _paginate_accounting(
        self, path: str, key: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """Collect `key` across pages of an Accounting-service response, which
        nests both the items and the page count under response.result."""
        items: list[dict[str, Any]] = []
        page = 1
        while page <= MAX_PAGES:
            query = dict(params or {})
            query.update({"per_page": PER_PAGE, "page": page})
            result = _accounting_result(self._request("GET", path, params=query))
            items.extend(result.get(key) or [])
            pages = int(result.get("pages") or 1)
            if page >= pages:
                break
            page += 1
        return items

    # ------------------------------------------------------------------
    # identity
    # ------------------------------------------------------------------

    def get_identity(self) -> dict[str, Any]:
        """Return the authenticated user: id, name, email, business_memberships."""
        data = self._request("GET", "/auth/api/v1/users/me") or {}
        return data.get("response") or {}

    # ------------------------------------------------------------------
    # projects & clients
    # ------------------------------------------------------------------

    def list_projects(self, business_id: int, active_only: bool = True) -> list[dict[str, Any]]:
        projects = self._paginate(f"/projects/business/{business_id}/projects", "projects")
        if active_only:
            projects = [p for p in projects if p.get("active", True) and not p.get("complete")]
        return projects

    def list_clients(self, account_id: str) -> list[dict[str, Any]]:
        return self._paginate_accounting(
            f"/accounting/account/{account_id}/users/clients", "clients"
        )

    # ------------------------------------------------------------------
    # time entries
    # ------------------------------------------------------------------

    def list_time_entries(
        self,
        business_id: int,
        started_from: str | None = None,
        started_to: str | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if started_from:
            params["started_from"] = started_from
        if started_to:
            params["started_to"] = started_to
        return self._paginate(
            f"/timetracking/business/{business_id}/time_entries", "time_entries", params
        )

    def get_time_entry(self, business_id: int, entry_id: int) -> dict[str, Any] | None:
        """Return one time entry, or None if FreshBooks no longer has it."""
        data = self._request(
            "GET",
            f"/timetracking/business/{business_id}/time_entries/{entry_id}",
            missing_ok=True,
        )
        if data is None:
            return None
        return data.get("time_entry") or None

    def create_time_entry(self, business_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        data = self._request(
            "POST",
            f"/timetracking/business/{business_id}/time_entries",
            json_body={"time_entry": payload},
        ) or {}
        return data.get("time_entry") or {}

    def update_time_entry(
        self, business_id: int, entry_id: int, payload: dict[str, Any]
    ) -> dict[str, Any]:
        data = self._request(
            "PUT",
            f"/timetracking/business/{business_id}/time_entries/{entry_id}",
            json_body={"time_entry": payload},
        ) or {}
        return data.get("time_entry") or {}

    def delete_time_entry(self, business_id: int, entry_id: int) -> None:
        self._request("DELETE", f"/timetracking/business/{business_id}/time_entries/{entry_id}")

    # ------------------------------------------------------------------
    # expenses
    # ------------------------------------------------------------------

    def list_expense_categories(self, account_id: str) -> list[dict[str, Any]]:
        return self._paginate_accounting(
            f"/accounting/account/{account_id}/expenses/categories", "categories"
        )

    def list_expenses(
        self,
        account_id: str,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> list[dict[str, Any]]:
        """Expenses dated within [date_from, date_to], both YYYY-MM-DD and inclusive."""
        params: dict[str, Any] = {}
        if date_from:
            params["search[date_min]"] = date_from
        if date_to:
            params["search[date_max]"] = date_to
        return self._paginate_accounting(
            f"/accounting/account/{account_id}/expenses/expenses", "expenses", params
        )

    def get_expense(self, account_id: str, expense_id: int) -> dict[str, Any] | None:
        """Return one expense, or None if FreshBooks no longer has it. A soft-deleted
        expense is still returned, with vis_state == 1."""
        data = self._request(
            "GET",
            f"/accounting/account/{account_id}/expenses/expenses/{expense_id}",
            missing_ok=True,
        )
        if data is None:
            return None
        return _accounting_result(data).get("expense") or None

    def create_expense(self, account_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        data = self._request(
            "POST",
            f"/accounting/account/{account_id}/expenses/expenses",
            json_body={"expense": payload},
        )
        return _accounting_result(data).get("expense") or {}

    def update_expense(
        self, account_id: str, expense_id: int, payload: dict[str, Any]
    ) -> dict[str, Any]:
        data = self._request(
            "PUT",
            f"/accounting/account/{account_id}/expenses/expenses/{expense_id}",
            json_body={"expense": payload},
        )
        return _accounting_result(data).get("expense") or {}

    def delete_expense(self, account_id: str, expense_id: int) -> None:
        """Accounting-service resources have no DELETE verb; they are soft-deleted
        by setting vis_state to 1."""
        self.update_expense(account_id, expense_id, {"vis_state": 1})


def _accounting_result(data: Any) -> dict[str, Any]:
    """Unwrap the Accounting service's {"response": {"result": {...}}} envelope."""
    return (((data or {}).get("response") or {}).get("result")) or {}
