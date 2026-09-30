"""log_expenses' upsert semantics and the expense listing, driven against an
in-memory FreshBooks Accounting service."""

from __future__ import annotations

import json
import time

import httpx
import pytest

from freshbooks_mcp import server, store
from freshbooks_mcp.api import FreshBooksClient

BUSINESS_ID = 77
ACCOUNT_ID = "acct123"
PROJECT_ID = 123
CLIENT_ID = 55
IDENTITY_ID = 9001
STAFF_ID = 1
TRAVEL = 4001
MEALS = 4002
CUSTOM_MEALS = 4003  # a custom "Meals" under "Other Expenses"
OTHER = 4004
TRAVEL_COGS = 4005  # the deprecated cost-of-goods-sold copy of "Travel"
AIRFARE = 4006
AIRFARE_COGS = 4007
DELETED_CATEGORY = 4999
DAY = "2026-09-01"
ACCOUNT = f"/accounting/account/{ACCOUNT_ID}"
EXPENSES = f"{ACCOUNT}/expenses/expenses"
CATEGORIES = f"{ACCOUNT}/expenses/categories"
PROJECTS = f"/projects/business/{BUSINESS_ID}/projects"
ME = "/auth/api/v1/users/me"


def envelope(result: dict) -> dict:
    return {"response": {"result": result}}


IDENTITY = {
    "id": IDENTITY_ID,
    "first_name": "Tim",
    "last_name": "Tester",
    "email": "tim@example.com",
    "roles": [
        {"id": 1, "role": "admin", "systemid": 5, "userid": STAFF_ID, "accountid": ACCOUNT_ID},
        {"id": 2, "role": "staff", "systemid": 6, "userid": 42, "accountid": "other"},
    ],
    "business_memberships": [
        {
            "role": "owner",
            "business": {"id": BUSINESS_ID, "name": "Acme", "account_id": ACCOUNT_ID},
        }
    ],
}


class FakeFreshBooks:
    """Minimal stand-in for the Accounting service's expenses and categories,
    plus the projects and identity endpoints log_expenses leans on."""

    def __init__(self) -> None:
        self.expenses: dict[int, dict] = {}
        self.next_id = 7000
        # (method, path, json body, query params)
        self.requests: list[tuple[str, str, dict | None, dict[str, str]]] = []

    def seed(self, expense_id: int, **fields) -> dict:
        expense = {
            "id": expense_id,
            "vis_state": 0,
            "status": 0,
            "clientid": 0,
            "projectid": 0,
            "has_receipt": False,
            **fields,
        }
        self.expenses[expense_id] = expense
        return expense

    def calls(self, method: str) -> list[tuple[str, str, dict | None, dict[str, str]]]:
        return [call for call in self.requests if call[0] == method]

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        method, path = request.method, request.url.path
        body = json.loads(request.content) if request.content else None
        params = dict(request.url.params)
        self.requests.append((method, path, body, params))

        assert request.headers["Authorization"] == "Bearer AT"
        if path.startswith("/accounting/"):
            assert request.headers["Api-Version"] == "alpha"

        if method == "GET" and path == ME:
            return httpx.Response(200, json={"response": IDENTITY})

        if method == "GET" and path == PROJECTS:
            return httpx.Response(
                200,
                json={
                    "projects": [
                        {
                            "id": PROJECT_ID,
                            "title": "Acme Rebuild",
                            "client_id": CLIENT_ID,
                            "active": True,
                            "complete": False,
                        }
                    ],
                    "meta": {"page": 1, "pages": 1, "total": 1},
                },
            )

        if method == "GET" and path == CATEGORIES:
            def cat(name, cid, parent=None, cogs=False, vis=0):
                return {
                    "category": name,
                    "categoryid": cid,
                    "id": cid,
                    "parentid": parent,
                    "is_cogs": cogs,
                    "vis_state": vis,
                }

            categories = [
                cat("Travel", TRAVEL),
                cat("Travel", TRAVEL_COGS, cogs=True),
                cat("Airfare", AIRFARE, parent=TRAVEL),
                cat("Airfare", AIRFARE_COGS, parent=TRAVEL_COGS, cogs=True),
                cat("Meals", MEALS),
                cat("Other Expenses", OTHER),
                cat("Meals", CUSTOM_MEALS, parent=OTHER),
                cat("Retired", DELETED_CATEGORY, vis=1),
            ]
            return httpx.Response(
                200,
                json=envelope(
                    {"categories": categories, "page": 1, "pages": 1, "per_page": 100, "total": 3}
                ),
            )

        if path == EXPENSES:
            if method == "POST":
                payload = body["expense"]
                expense_id = self.next_id
                self.next_id += 1
                self.expenses[expense_id] = {
                    "id": expense_id,
                    "vis_state": 0,
                    "status": 1 if payload.get("clientid") else 0,
                    "has_receipt": False,
                    **payload,
                }
                return httpx.Response(200, json=envelope({"expense": self.expenses[expense_id]}))
            if method == "GET":
                date_min = params.get("search[date_min]")
                date_max = params.get("search[date_max]")
                rows = [
                    e
                    for e in self.expenses.values()
                    if (not date_min or e["date"] >= date_min)
                    and (not date_max or e["date"] <= date_max)
                ]
                return httpx.Response(
                    200,
                    json=envelope(
                        {"expenses": rows, "page": 1, "pages": 1, "per_page": 100, "total": len(rows)}
                    ),
                )

        if path.startswith(EXPENSES + "/"):
            expense_id = int(path.rsplit("/", 1)[1])
            if method == "GET":
                if expense_id not in self.expenses:
                    return httpx.Response(
                        404, json={"response": {"errors": [{"message": "Expense not found."}]}}
                    )
                return httpx.Response(200, json=envelope({"expense": self.expenses[expense_id]}))
            if method == "PUT":
                self.expenses[expense_id].update(body["expense"])
                return httpx.Response(200, json=envelope({"expense": self.expenses[expense_id]}))

        return httpx.Response(500, json={"unexpected": f"{method} {path}"})


@pytest.fixture
def fake(tmp_path, monkeypatch):
    monkeypatch.setenv(store.STATE_DIR_ENV, str(tmp_path / "state"))
    store.save_tokens(
        {
            "access_token": "AT",
            "refresh_token": "RT",
            "expires_at": time.time() + 3600,
            "business_id": BUSINESS_ID,
            "account_id": ACCOUNT_ID,
            "identity_id": IDENTITY_ID,
        }
    )
    store.save_mapping(
        {"acme": {"project_id": PROJECT_ID, "client_id": CLIENT_ID, "project_title": "Acme Rebuild"}}
    )

    freshbooks = FakeFreshBooks()
    monkeypatch.setattr(
        server, "_make_client", lambda: FreshBooksClient(transport=freshbooks.transport)
    )
    return freshbooks


def log(**overrides) -> dict:
    entry = {
        "ref": "uber-1",
        "date": DAY,
        "amount": 45,
        "category": "travel",
        "vendor": "Uber",
        "notes": "Airport",
        "label": "acme",
    }
    entry.update(overrides)
    entry = {k: v for k, v in entry.items() if v is not None}
    return server.log_expenses([entry])


def test_first_call_creates_the_expense_and_learns_the_staff_id(fake):
    result = log()

    assert result["summary"] == {"created": 1, "updated": 0, "unchanged": 0, "failed": 0}
    assert result["results"][0]["action"] == "created"
    assert result["results"][0]["ref"] == "uber-1"
    expense_id = result["results"][0]["expense_id"]

    posts = fake.calls("POST")
    assert len(posts) == 1
    assert posts[0][2]["expense"] == {
        "amount": {"amount": "45.00"},
        "categoryid": TRAVEL,
        "date": DAY,
        "vendor": "Uber",
        "notes": "Airport",
        "clientid": CLIENT_ID,
        "projectid": PROJECT_ID,
        "staffid": STAFF_ID,
    }
    assert store.load_expense_ledger() == {"uber-1": expense_id}
    # The staff id came from the identity's role on this account and is now cached.
    assert len([c for c in fake.calls("GET") if c[1] == ME]) == 1
    assert store.load_tokens()["staff_id"] == STAFF_ID


def test_cached_staff_id_skips_the_identity_lookup(fake):
    tokens = store.load_tokens()
    tokens["staff_id"] = 7
    store.save_tokens(tokens)

    log()

    assert fake.calls("POST")[0][2]["expense"]["staffid"] == 7
    assert not any(c[1] == ME for c in fake.requests)


def test_repeat_of_identical_expense_is_unchanged_and_writes_nothing(fake):
    first = log()
    fake.requests.clear()

    second = log()

    assert second["summary"] == {"created": 0, "updated": 0, "unchanged": 1, "failed": 0}
    assert second["results"][0]["expense_id"] == first["results"][0]["expense_id"]
    assert fake.calls("POST") == []
    assert fake.calls("PUT") == []


def test_changed_amount_with_the_same_ref_updates_the_same_expense(fake):
    expense_id = log()["results"][0]["expense_id"]
    fake.requests.clear()

    result = log(amount="48.5", notes="Airport, with tip")

    assert result["summary"] == {"created": 0, "updated": 1, "unchanged": 0, "failed": 0}
    assert result["results"][0]["expense_id"] == expense_id
    assert fake.calls("POST") == []

    puts = fake.calls("PUT")
    assert len(puts) == 1
    assert puts[0][1] == f"{EXPENSES}/{expense_id}"
    assert puts[0][2]["expense"]["amount"] == {"amount": "48.50"}
    assert puts[0][2]["expense"]["notes"] == "Airport, with tip"
    assert "staffid" not in puts[0][2]["expense"]
    assert store.load_expense_ledger() == {"uber-1": expense_id}


def test_ref_defaults_to_date_vendor_and_amount(fake):
    first = log(ref=None)
    assert first["results"][0]["ref"] == f"{DAY}|Uber|45.00"

    second = log(ref=None)
    assert second["results"][0]["action"] == "unchanged"

    third = log(ref=None, amount=46)
    assert third["results"][0]["action"] == "created"
    assert third["results"][0]["ref"] == f"{DAY}|Uber|46.00"


def test_category_path_is_case_insensitive_and_unknown_fails_only_its_entry(fake):
    result = server.log_expenses(
        [
            {"date": DAY, "amount": 12, "category": "Nope", "vendor": "Cafe"},
            {"date": DAY, "amount": 12, "category": "other expenses>MEALS", "vendor": "Cafe"},
            {"date": DAY, "amount": 12, "category_id": DELETED_CATEGORY, "vendor": "Cafe"},
        ]
    )

    assert result["summary"] == {"created": 1, "updated": 0, "unchanged": 0, "failed": 2}
    assert "Nope" in result["results"][0]["error"]
    assert "Travel" in result["results"][0]["error"]
    assert result["results"][1]["action"] == "created"
    assert fake.calls("POST")[0][2]["expense"]["categoryid"] == CUSTOM_MEALS
    assert str(DELETED_CATEGORY) in result["results"][2]["error"]


def test_regular_category_wins_over_its_cost_of_goods_sold_twin(fake):
    server.log_expenses(
        [
            {"date": DAY, "amount": 1, "category": "travel"},
            {"date": DAY, "amount": 2, "category": "airfare"},
            {"date": DAY, "amount": 3, "category": "Travel > Airfare"},
            {"date": DAY, "amount": 4, "category_id": AIRFARE_COGS},
        ]
    )

    posted = [call[2]["expense"]["categoryid"] for call in fake.calls("POST")]
    assert posted == [TRAVEL, AIRFARE, AIRFARE, AIRFARE_COGS]


def test_ambiguous_category_name_lists_the_paths(fake):
    result = server.log_expenses([{"date": DAY, "amount": 12, "category": "meals"}])

    error = result["results"][0]["error"]
    assert result["summary"]["failed"] == 1
    assert "'Meals' (id 4002)" in error
    assert "'Other Expenses > Meals' (id 4003)" in error
    assert fake.calls("POST") == []


def test_expense_without_a_client_is_internal(fake):
    log(label=None)

    payload = fake.calls("POST")[0][2]["expense"]
    assert payload["clientid"] == 0
    assert payload["projectid"] == 0


def test_bare_client_id_bills_without_a_project(fake):
    log(label=None, client_id=CLIENT_ID)

    payload = fake.calls("POST")[0][2]["expense"]
    assert payload["clientid"] == CLIENT_ID
    assert payload["projectid"] == 0


def test_currency_and_markup_are_sent_and_compared(fake):
    log(currency="eur", markup_percent=10)

    payload = fake.calls("POST")[0][2]["expense"]
    assert payload["amount"] == {"amount": "45.00", "code": "EUR"}
    assert payload["markup_percent"] == "10"

    assert log(currency="eur", markup_percent=10)["results"][0]["action"] == "unchanged"
    assert log(currency="eur", markup_percent=15)["results"][0]["action"] == "updated"


def test_bad_amounts_fail_their_own_entry(fake):
    result = server.log_expenses(
        [
            {"date": DAY, "amount": 0, "category": "travel"},
            {"date": DAY, "amount": "lots", "category": "travel"},
            {"date": DAY, "category": "travel"},
        ]
    )

    assert result["summary"]["failed"] == 3
    assert "positive" in result["results"][0]["error"]
    assert "not a number" in result["results"][1]["error"]
    assert "amount" in result["results"][2]["error"]
    assert fake.calls("POST") == []


def test_hand_entered_expense_is_never_touched(fake):
    hand_entered = fake.seed(
        999,
        amount={"amount": "45.00", "code": "USD"},
        categoryid=TRAVEL,
        date=DAY,
        vendor="Uber",
        notes="typed straight into FreshBooks",
        staffid=STAFF_ID,
    )
    snapshot = dict(hand_entered)

    result = log()

    assert result["results"][0]["action"] == "created"
    assert result["results"][0]["expense_id"] != 999
    assert fake.expenses[999] == snapshot
    assert not any(call[1].endswith("/999") for call in fake.requests if call[0] != "GET")


def test_delete_refuses_an_expense_not_in_the_ledger(fake):
    fake.seed(999, amount={"amount": "1.00"}, categoryid=TRAVEL, date=DAY)

    result = server.delete_expense(999)

    assert "999" in result["error"]
    assert fake.calls("PUT") == []
    assert fake.expenses[999]["vis_state"] == 0


def test_delete_soft_deletes_a_ledger_owned_expense(fake):
    expense_id = log()["results"][0]["expense_id"]

    result = server.delete_expense(expense_id)

    assert result == {"deleted": True, "expense_id": expense_id}
    puts = fake.calls("PUT")
    assert len(puts) == 1
    assert puts[0][1] == f"{EXPENSES}/{expense_id}"
    assert puts[0][2] == {"expense": {"vis_state": 1}}
    assert fake.expenses[expense_id]["vis_state"] == 1
    assert store.load_expense_ledger() == {}


def test_expense_deleted_in_freshbooks_is_recreated(fake):
    expense_id = log()["results"][0]["expense_id"]
    fake.expenses[expense_id]["vis_state"] = 1  # deleted in the FreshBooks UI
    fake.requests.clear()

    result = log()

    assert result["results"][0]["action"] == "created"
    new_id = result["results"][0]["expense_id"]
    assert new_id != expense_id
    assert fake.calls("PUT") == []
    assert store.load_expense_ledger() == {"uber-1": new_id}


def test_list_expenses_flags_ownership_names_categories_and_hides_deleted(fake):
    fake.seed(
        999,
        amount={"amount": "18.25", "code": "USD"},
        categoryid=CUSTOM_MEALS,
        date=DAY,
        vendor="Cafe",
        notes="lunch",
    )
    fake.seed(998, amount={"amount": "5.00"}, categoryid=MEALS, date=DAY, vis_state=1)
    fake.seed(997, amount={"amount": "5.00"}, categoryid=MEALS, date="2026-08-01")
    ours = log()["results"][0]["expense_id"]
    fake.requests.clear()

    listed = server.list_expenses(DAY, DAY)["expenses"]
    by_id = {expense["id"]: expense for expense in listed}

    assert set(by_id) == {999, ours}
    assert by_id[ours]["owned_by_ledger"] is True
    assert by_id[ours]["ref"] == "uber-1"
    assert by_id[ours]["category"] == "Travel"
    assert by_id[ours]["status"] == "outstanding"
    assert by_id[ours]["client_id"] == CLIENT_ID
    assert by_id[ours]["project_id"] == PROJECT_ID
    assert by_id[999]["owned_by_ledger"] is False
    assert "ref" not in by_id[999]
    assert by_id[999]["category"] == "Other Expenses > Meals"
    assert by_id[999]["status"] == "internal"
    assert by_id[999]["client_id"] is None
    assert by_id[999]["amount"] == "18.25"
    assert by_id[999]["currency"] == "USD"

    params = next(call for call in fake.calls("GET") if call[1] == EXPENSES)[3]
    assert params["search[date_min]"] == DAY
    assert params["search[date_max]"] == DAY


def test_list_expense_categories_hides_deleted_and_sorts_by_path(fake):
    result = server.list_expense_categories()

    def row(cid, name, path, parent=None, cogs=False):
        return {
            "category_id": cid,
            "name": name,
            "path": path,
            "parent_id": parent,
            "is_cogs": cogs,
        }

    assert result == {
        "categories": [
            row(MEALS, "Meals", "Meals"),
            row(OTHER, "Other Expenses", "Other Expenses"),
            row(CUSTOM_MEALS, "Meals", "Other Expenses > Meals", parent=OTHER),
            row(TRAVEL, "Travel", "Travel"),
            row(TRAVEL_COGS, "Travel", "Travel", cogs=True),
            row(AIRFARE, "Airfare", "Travel > Airfare", parent=TRAVEL),
            row(AIRFARE_COGS, "Airfare", "Travel > Airfare", parent=TRAVEL_COGS, cogs=True),
        ]
    }


def test_submit_auth_code_records_the_staff_id(fake, monkeypatch):
    monkeypatch.setattr(server.auth, "exchange_code", lambda code: store.load_tokens())

    result = server.submit_auth_code("code")

    assert result["active_business"]["business_id"] == BUSINESS_ID
    assert store.load_tokens()["staff_id"] == STAFF_ID


def test_insufficient_scope_errors_explain_how_to_fix_the_app(fake, monkeypatch):
    def forbidden(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={
                "response": {
                    "errors": [
                        {
                            "message": "403 Forbidden: insufficient_scope: Required: "
                            "['user:expenses:read'].",
                            "errno": 403,
                        }
                    ]
                }
            },
        )

    monkeypatch.setattr(
        server,
        "_make_client",
        lambda: FreshBooksClient(transport=httpx.MockTransport(forbidden)),
    )

    result = server.list_expense_categories()

    assert "user:expenses:read" in result["error"]
    assert "my.freshbooks.com/#/developer" in result["error"]
    assert "submit_auth_code" in result["error"]
