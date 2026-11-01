"""Invoice tools, driven against an in-memory FreshBooks Accounting invoices
service plus the projects and time tracking endpoints they lean on."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone

import httpx
import pytest

from freshbooks_mcp import server, store
from freshbooks_mcp.api import FreshBooksClient

BUSINESS_ID = 77
ACCOUNT_ID = "acct123"
PROJECT_ID = 123
FIXED_PROJECT_ID = 124
CLIENT_ID = 55
IDENTITY_ID = 9001
DEV_SERVICE = 777
DESIGN_SERVICE = 778
ACCOUNT = f"/accounting/account/{ACCOUNT_ID}"
INVOICES = f"{ACCOUNT}/invoices/invoices"
TIME_ENTRIES = f"/timetracking/business/{BUSINESS_ID}/time_entries"
PROJECT_DETAIL = f"/projects/business/{BUSINESS_ID}/project/"
ME = "/auth/api/v1/users/me"


def envelope(result: dict) -> dict:
    return {"response": {"result": result}}


def local_started_at(day: str, hour: int = 9) -> str:
    year, month, dom = (int(part) for part in day.split("-"))
    local = datetime(year, month, dom, hour).astimezone()
    return local.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class FakeFreshBooks:
    """Minimal stand-in for the invoices, projects and time entry endpoints."""

    def __init__(self) -> None:
        self.invoices: dict[int, dict] = {}
        self.entries: dict[int, dict] = {}
        self.next_id = 9000
        self.next_number = 1
        # When set, POSTs whose lines carry these fields are rejected with 422.
        self.reject_linkage = False
        # When True, a successful POST with modern_time_entries flips billed.
        self.link_marks_billed = True
        self.requests: list[tuple[str, str, dict | None, dict[str, str]]] = []

    def seed_invoice(self, invoice_id: int, **fields) -> dict:
        invoice = {
            "id": invoice_id,
            "invoiceid": invoice_id,
            "invoice_number": f"H{invoice_id}",
            "customerid": CLIENT_ID,
            "organization": "Acme",
            "create_date": "2026-10-01",
            "due_date": "2026-10-31",
            "v3_status": "draft",
            "status": 1,
            "vis_state": 0,
            "amount": {"amount": "100.00", "code": "USD"},
            "outstanding": {"amount": "100.00", "code": "USD"},
            "currency_code": "USD",
            "lines": [],
            **fields,
        }
        self.invoices[invoice_id] = invoice
        return invoice

    def seed_entry(self, entry_id: int, day: str, minutes: int, **fields) -> dict:
        entry = {
            "id": entry_id,
            "started_at": local_started_at(day),
            "duration": minutes * 60,
            "project_id": PROJECT_ID,
            "client_id": CLIENT_ID,
            "service_id": DEV_SERVICE,
            "note": f"work on {day}",
            "billable": True,
            "billed": False,
            "active": True,
            **fields,
        }
        self.entries[entry_id] = entry
        return entry

    def calls(self, method: str, path: str | None = None) -> list[tuple]:
        return [
            c for c in self.requests if c[0] == method and (path is None or c[1] == path)
        ]

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def _project(self, project_id: int) -> dict | None:
        if project_id == PROJECT_ID:
            return {
                "id": PROJECT_ID,
                "title": "Acme Rebuild",
                "client_id": CLIENT_ID,
                "project_type": "hourly_rate",
                "rate": "150",
                "services": [
                    {"id": DEV_SERVICE, "name": "Development", "billable": True},
                    {"id": DESIGN_SERVICE, "name": "Design", "billable": True},
                ],
            }
        if project_id == FIXED_PROJECT_ID:
            return {
                "id": FIXED_PROJECT_ID,
                "title": "Acme Fixed",
                "client_id": CLIENT_ID,
                "project_type": "fixed_price",
                "rate": None,
                "services": [],
            }
        return None

    def handle(self, request: httpx.Request) -> httpx.Response:
        method, path = request.method, request.url.path
        body = json.loads(request.content) if request.content else None
        params = dict(request.url.params)
        self.requests.append((method, path, body, params))

        assert request.headers["Authorization"] == "Bearer AT"
        if path.startswith("/accounting/"):
            assert request.headers["Api-Version"] == "alpha"

        if method == "GET" and path == ME:
            return httpx.Response(
                200, json={"response": {"id": IDENTITY_ID, "first_name": "Tim", "last_name": "Ritchey"}}
            )

        if method == "GET" and path.startswith(PROJECT_DETAIL):
            project = self._project(int(path.rsplit("/", 1)[1]))
            if project is None:
                return httpx.Response(404, json={"error": "not found"})
            return httpx.Response(200, json={"project": project})

        if path == TIME_ENTRIES and method == "GET":
            return httpx.Response(
                200,
                json={
                    "time_entries": list(self.entries.values()),
                    "meta": {"page": 1, "pages": 1, "total": len(self.entries)},
                },
            )
        if path.startswith(TIME_ENTRIES + "/") and method == "GET":
            entry_id = int(path.rsplit("/", 1)[1])
            if entry_id not in self.entries:
                return httpx.Response(404, json={"error": "not found"})
            return httpx.Response(200, json={"time_entry": self.entries[entry_id]})

        if path == INVOICES:
            if method == "GET":
                rows = list(self.invoices.values())
                if "search[customerid]" in params:
                    rows = [r for r in rows if str(r["customerid"]) == params["search[customerid]"]]
                if "search[v3_status]" in params:
                    rows = [r for r in rows if r["v3_status"] == params["search[v3_status]"]]
                return httpx.Response(
                    200,
                    json=envelope(
                        {"invoices": rows, "page": 1, "pages": 1, "per_page": 100, "total": len(rows)}
                    ),
                )
            if method == "POST":
                payload = body["invoice"]
                linked = any(
                    "modern_time_entries" in line or "modern_project_id" in line
                    for line in payload.get("lines", [])
                )
                if linked and self.reject_linkage:
                    return httpx.Response(
                        422,
                        json={
                            "response": {
                                "errors": [
                                    {"message": "Invalid field modern_time_entries", "field": "modern_time_entries"}
                                ]
                            }
                        },
                    )
                invoice_id = self.next_id
                self.next_id += 1
                total = sum(
                    float(line["qty"]) * float(line["unit_cost"]["amount"])
                    for line in payload.get("lines", [])
                )
                lines = [
                    {**line, "lineid": n + 1, "amount": {"amount": f"{float(line['qty']) * float(line['unit_cost']['amount']):.2f}", "code": "USD"}}
                    for n, line in enumerate(payload.get("lines", []))
                ]
                invoice = {
                    "id": invoice_id,
                    "invoiceid": invoice_id,
                    "invoice_number": f"{self.next_number:07d}",
                    "organization": "Acme",
                    "due_date": payload["create_date"],
                    "v3_status": "draft",
                    "status": 1,
                    "vis_state": 0,
                    "amount": {"amount": f"{total:.2f}", "code": "USD"},
                    "outstanding": {"amount": f"{total:.2f}", "code": "USD"},
                    **{k: v for k, v in payload.items() if k != "lines"},
                    "lines": lines,
                }
                self.next_number += 1
                self.invoices[invoice_id] = invoice
                if self.link_marks_billed:
                    for line in payload.get("lines", []):
                        for entry_id in line.get("modern_time_entries", []):
                            if int(entry_id) in self.entries:
                                self.entries[int(entry_id)]["billed"] = True
                # The real create response omits lines unless include[]=lines is asked for.
                return httpx.Response(
                    200, json=envelope({"invoice": {k: v for k, v in invoice.items() if k != "lines"}})
                )

        if path.startswith(INVOICES + "/"):
            invoice_id = int(path.rsplit("/", 1)[1])
            if invoice_id not in self.invoices:
                return httpx.Response(
                    404, json={"response": {"errors": [{"message": "Invoice not found."}]}}
                )
            invoice = self.invoices[invoice_id]
            if method == "GET":
                assert params.get("include[]") == "lines"
                return httpx.Response(200, json=envelope({"invoice": invoice}))
            if method == "PUT":
                change = dict(body["invoice"])
                if change.pop("action_mark_as_sent", False):
                    invoice["v3_status"] = "sent"
                    invoice["status"] = 2
                invoice.update(change)
                return httpx.Response(200, json=envelope({"invoice": invoice}))

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


def no_email_anywhere(fake: FakeFreshBooks) -> None:
    for _method, _path, body, _params in fake.requests:
        text = json.dumps(body or {})
        assert "action_email" not in text
        assert "email_recipients" not in text


# --------------------------------------------------------------------------
# list / get
# --------------------------------------------------------------------------


def test_list_flags_ledger_ownership_and_hides_deleted(fake):
    fake.seed_invoice(1, invoice_number="0000001")
    fake.seed_invoice(2, invoice_number="0000002", v3_status="paid",
                      outstanding={"amount": "0.00", "code": "USD"})
    fake.seed_invoice(3, vis_state=1)
    store.set_invoice_ledger_entry("my-ref", 2)

    result = server.list_invoices(date_from="2026-10-01", date_to="2026-10-31")

    invoices = {i["invoice_id"]: i for i in result["invoices"]}
    assert set(invoices) == {1, 2}
    assert invoices[1]["owned_by_ledger"] is False and "ref" not in invoices[1]
    assert invoices[2]["owned_by_ledger"] is True and invoices[2]["ref"] == "my-ref"
    assert invoices[2]["status"] == "paid"
    assert invoices[2]["outstanding"] == "0.00"
    assert invoices[1]["amount"] == "100.00"
    assert invoices[1]["client"] == "Acme"
    params = fake.calls("GET", INVOICES)[0][3]
    assert params["search[date_min]"] == "2026-10-01"
    assert params["search[date_max]"] == "2026-10-31"
    assert "include[]" not in params


def test_list_passes_client_and_status_filters(fake):
    fake.seed_invoice(1)
    server.list_invoices(client_id=CLIENT_ID, status="draft")
    params = fake.calls("GET", INVOICES)[0][3]
    assert params["search[customerid]"] == str(CLIENT_ID)
    assert params["search[v3_status]"] == "draft"


def test_get_invoice_reports_lines_and_time_entry_ids(fake):
    fake.seed_invoice(
        5,
        notes="Thanks",
        lines=[
            {"lineid": 1, "name": "Dev", "description": "x", "qty": 2,
             "unit_cost": {"amount": "150.00", "code": "USD"},
             "amount": {"amount": "300.00", "code": "USD"},
             "modern_project_id": PROJECT_ID, "modern_time_entries": [11, 12]},
            {"lineid": 2, "name": "Misc", "qty": 1,
             "unit_cost": {"amount": "5", "code": "USD"}, "amount": {"amount": "5", "code": "USD"}},
        ],
    )
    result = server.get_invoice(5)
    assert result["notes"] == "Thanks"
    assert result["lines"][0]["time_entry_ids"] == [11, 12]
    assert result["lines"][0]["unit_cost"] == "150.00"
    assert result["lines"][1]["time_entry_ids"] == []
    assert result["lines"][1]["amount"] == "5.00"


# --------------------------------------------------------------------------
# create_invoice
# --------------------------------------------------------------------------


def test_create_invoice_builds_payload_without_nulls(fake):
    result = server.create_invoice(
        client_id=CLIENT_ID,
        create_date="2026-10-31",
        lines=[
            {"name": "Consulting", "qty": 3, "unit_cost": 150},
            {"name": "Dev", "description": "Sprint", "qty": 1.5, "unit_cost": "99.5",
             "project_id": PROJECT_ID, "time_entry_ids": [11, "12"]},
        ],
        ref="oct-acme",
    )

    assert result["action"] == "created"
    assert result["marked_sent"] is False
    posts = fake.calls("POST", INVOICES)
    assert len(posts) == 1
    payload = posts[0][2]["invoice"]
    assert payload == {
        "customerid": CLIENT_ID,
        "create_date": "2026-10-31",
        "currency_code": "USD",
        "lines": [
            {"name": "Consulting", "description": "", "qty": 3,
             "unit_cost": {"amount": "150.00", "code": "USD"}},
            {"name": "Dev", "description": "Sprint", "qty": 1.5,
             "unit_cost": {"amount": "99.50", "code": "USD"},
             "modern_project_id": PROJECT_ID, "modern_time_entries": [11, 12]},
        ],
    }
    invoice = result["invoice"]
    assert invoice["status"] == "draft"
    assert invoice["owned_by_ledger"] is True and invoice["ref"] == "oct-acme"
    assert len(invoice["lines"]) == 2
    assert store.load_invoice_ledger() == {"oct-acme": invoice["invoice_id"]}
    assert not fake.calls("PUT")
    no_email_anywhere(fake)


def test_create_invoice_passes_line_type_through(fake):
    server.create_invoice(
        client_id=CLIENT_ID,
        lines=[{"name": "Development", "qty": 2, "unit_cost": 150, "type": 22,
                "project_id": PROJECT_ID, "time_entry_ids": [11]}],
    )
    (line,) = fake.calls("POST", INVOICES)[0][2]["invoice"]["lines"]
    assert line["type"] == 22
    assert line["modern_time_entries"] == [11]


def test_create_invoice_resolves_client_from_label_and_keys_ledger_by_id(fake):
    result = server.create_invoice(label="acme", lines=[{"name": "X", "qty": 1, "unit_cost": 1}])
    assert fake.calls("POST", INVOICES)[0][2]["invoice"]["customerid"] == CLIENT_ID
    invoice_id = result["invoice"]["invoice_id"]
    assert store.load_invoice_ledger() == {str(invoice_id): invoice_id}


def test_create_invoice_requires_a_client(fake):
    result = server.create_invoice(lines=[{"name": "X", "qty": 1, "unit_cost": 1}])
    assert "client_id" in result["error"]
    assert not fake.calls("POST")


def test_create_invoice_with_existing_ref_writes_nothing(fake):
    lines = [{"name": "X", "qty": 1, "unit_cost": 10}]
    first = server.create_invoice(client_id=CLIENT_ID, lines=lines, ref="r1")
    second = server.create_invoice(client_id=CLIENT_ID, lines=lines, ref="r1")
    assert second["action"] == "exists"
    assert second["invoice"]["invoice_id"] == first["invoice"]["invoice_id"]
    assert len(fake.calls("POST")) == 1


def test_create_invoice_mark_as_sent_puts_the_action_and_never_emails(fake):
    result = server.create_invoice(
        client_id=CLIENT_ID, lines=[{"name": "X", "qty": 1, "unit_cost": 10}], mark_as_sent=True
    )
    assert result["marked_sent"] is True
    assert result["invoice"]["status"] == "sent"
    puts = fake.calls("PUT")
    assert len(puts) == 1
    assert puts[0][2] == {"invoice": {"action_mark_as_sent": True}}
    no_email_anywhere(fake)


# --------------------------------------------------------------------------
# create_invoice_from_time
# --------------------------------------------------------------------------


def seed_october(fake: FakeFreshBooks) -> None:
    fake.seed_entry(11, "2026-10-02", 90, note="Built export")
    fake.seed_entry(12, "2026-10-01", 120, note="Fixed paging", service_id=DESIGN_SERVICE)
    fake.seed_entry(13, "2026-10-03", 60, billed=True)
    fake.seed_entry(14, "2026-10-04", 60, billable=False)
    fake.seed_entry(15, "2026-10-05", 60, active=False)
    fake.seed_entry(16, "2026-10-06", 60, project_id=999)
    fake.seed_entry(17, "2026-11-02", 60)  # outside the range


def test_from_time_per_entry_lines_and_ledger(fake):
    seed_october(fake)

    result = server.create_invoice_from_time(
        label="acme", started_from="2026-10-01", started_to="2026-10-31", create_date="2026-10-31"
    )

    assert result["action"] == "created"
    assert result["linked"] is True
    assert result["time_entries"] == {"count": 2, "total_hours": 3.5, "ids": [12, 11]}
    assert result["billed_after"] == 2
    assert result["marked_sent"] is False
    payload = fake.calls("POST", INVOICES)[0][2]["invoice"]
    assert payload["customerid"] == CLIENT_ID
    assert payload["lines"] == [
        {"type": 22, "name": "Design", "description": "2026-10-01: Fixed paging", "qty": 2.0,
         "unit_cost": {"amount": "150.00", "code": "USD"},
         "modern_project_id": PROJECT_ID, "modern_time_entries": [12]},
        {"type": 22, "name": "Development", "description": "2026-10-02: Built export", "qty": 1.5,
         "unit_cost": {"amount": "150.00", "code": "USD"},
         "modern_project_id": PROJECT_ID, "modern_time_entries": [11]},
    ]
    ref = f"time|{PROJECT_ID}|2026-10-01|2026-10-31"
    assert store.load_invoice_ledger() == {ref: result["invoice"]["invoice_id"]}
    assert result["invoice"]["ref"] == ref

    again = server.create_invoice_from_time(
        project_id=PROJECT_ID, started_from="2026-10-01", started_to="2026-10-31"
    )
    assert again["action"] == "exists"
    assert len(fake.calls("POST")) == 1
    no_email_anywhere(fake)


def test_from_time_single_groups_by_service_like_the_ui(fake):
    seed_october(fake)
    result = server.create_invoice_from_time(
        project_id=PROJECT_ID, started_from="2026-10-01", started_to="2026-10-31",
        line_style="single", include_billed=True,
    )
    lines = fake.calls("POST", INVOICES)[0][2]["invoice"]["lines"]
    assert lines == [
        {"type": 22, "name": "Design",
         "description": "(Acme Rebuild) Tim Ritchey \u2013 Oct 1, 2026 - Oct 1, 2026",
         "qty": "2", "unit_cost": {"amount": "150.00", "code": "USD"},
         "modern_project_id": PROJECT_ID, "modern_time_entries": [12]},
        {"type": 22, "name": "Development",
         "description": "(Acme Rebuild) Tim Ritchey \u2013 Oct 2, 2026 - Oct 3, 2026",
         "qty": "2.50", "unit_cost": {"amount": "150.00", "code": "USD"},
         "modern_project_id": PROJECT_ID, "modern_time_entries": [11, 13]},
    ]
    assert result["time_entries"] == {"count": 3, "total_hours": 4.5, "ids": [12, 11, 13]}
    # No name in tokens.json, so the identity was looked up live.
    assert len(fake.calls("GET", ME)) == 1


def test_single_description_matches_the_ui_byte_for_byte(fake):
    tokens = store.load_tokens()
    tokens.update({"first_name": "Tim", "last_name": "Ritchey"})
    store.save_tokens(tokens)
    fake.seed_entry(21, "2026-10-01", 600)
    fake.seed_entry(22, "2026-10-15", 480)
    fake.seed_entry(23, "2026-10-16", 60)  # outside the requested range

    server.create_invoice_from_time(
        project_id=PROJECT_ID, started_from="2026-09-28", started_to="2026-10-15",
        line_style="single",
    )

    (line,) = fake.calls("POST", INVOICES)[0][2]["invoice"]["lines"]
    expected = "(Acme Rebuild) Tim Ritchey \u2013 Oct 1, 2026 - Oct 15, 2026"
    assert line["description"].encode("utf-8") == expected.encode("utf-8")
    assert "\u2013".encode("utf-8") == b"\xe2\x80\x93"
    assert line["qty"] == "18"
    assert line["modern_time_entries"] == [21, 22]
    # Name came from tokens.json; no identity lookup.
    assert not fake.calls("GET", ME)


def test_hours_text_formats_whole_and_fractional():
    assert server._hours_text(88.0) == "88"
    assert server._hours_text(4.5) == "4.50"
    assert server._hours_text(1 / 3) == "0.33"


def test_from_time_strips_linkage_and_retries_when_rejected(fake):
    seed_october(fake)
    fake.reject_linkage = True
    result = server.create_invoice_from_time(
        label="acme", started_from="2026-10-01", started_to="2026-10-31"
    )
    assert result["action"] == "created"
    assert result["linked"] is False
    assert result["billed_after"] == 0
    posts = fake.calls("POST", INVOICES)
    assert len(posts) == 2
    for line in posts[1][2]["invoice"]["lines"]:
        assert "modern_time_entries" not in line and "modern_project_id" not in line
        assert "type" not in line


def test_from_time_mark_as_sent(fake):
    seed_october(fake)
    result = server.create_invoice_from_time(
        label="acme", started_from="2026-10-01", started_to="2026-10-31", mark_as_sent=True
    )
    assert result["marked_sent"] is True
    assert [c[2] for c in fake.calls("PUT")] == [{"invoice": {"action_mark_as_sent": True}}]
    no_email_anywhere(fake)


def test_from_time_nothing_to_bill(fake):
    fake.seed_entry(13, "2026-10-03", 60, billed=True)
    result = server.create_invoice_from_time(
        label="acme", started_from="2026-10-01", started_to="2026-10-31"
    )
    assert result["action"] == "nothing_to_bill"
    assert result["counts"]["already_billed"] == 1
    assert not fake.calls("POST")
    assert store.load_invoice_ledger() == {}


def test_from_time_refuses_fixed_price_and_bad_line_style(fake):
    fixed = server.create_invoice_from_time(
        project_id=FIXED_PROJECT_ID, started_from="2026-10-01", started_to="2026-10-31"
    )
    assert "hourly" in fixed["error"]
    bad = server.create_invoice_from_time(
        label="acme", started_from="2026-10-01", started_to="2026-10-31", line_style="daily"
    )
    assert "per_entry" in bad["error"] and "single" in bad["error"]
    assert not fake.calls("POST")


# --------------------------------------------------------------------------
# mark_invoice_sent / delete_invoice
# --------------------------------------------------------------------------


def test_mark_invoice_sent_flips_a_draft(fake):
    fake.seed_invoice(1)
    result = server.mark_invoice_sent(1)
    assert result == {"invoice_id": 1, "status": "sent", "previous_status": "draft"}
    assert fake.calls("PUT")[0][2] == {"invoice": {"action_mark_as_sent": True}}
    no_email_anywhere(fake)


def test_mark_invoice_sent_refuses_a_non_draft(fake):
    fake.seed_invoice(1, v3_status="sent", status=2)
    result = server.mark_invoice_sent(1)
    assert "not 'draft'" in result["error"]
    assert not fake.calls("PUT")


def test_delete_refuses_an_invoice_not_in_the_ledger(fake):
    fake.seed_invoice(1)
    result = server.delete_invoice(1)
    assert "Refusing" in result["error"] and "FreshBooks" in result["error"]
    assert not fake.calls("PUT")
    assert fake.invoices[1]["vis_state"] == 0


def test_delete_soft_deletes_a_ledger_invoice(fake):
    fake.seed_invoice(1)
    store.set_invoice_ledger_entry("mine", 1)
    result = server.delete_invoice(1)
    assert result == {"deleted": True, "invoice_id": 1}
    assert fake.calls("PUT")[0][2] == {"invoice": {"vis_state": 1}}
    assert fake.invoices[1]["vis_state"] == 1
    assert store.load_invoice_ledger() == {}
