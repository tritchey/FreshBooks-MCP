"""log_time's upsert semantics, driven against an in-memory FreshBooks."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from freshbooks_mcp import server, store
from freshbooks_mcp.api import FreshBooksClient

BUSINESS_ID = 77
PROJECT_ID = 123
CLIENT_ID = 55
IDENTITY_ID = 9001
DAY = "2026-08-24"
TIME_ENTRIES = f"/timetracking/business/{BUSINESS_ID}/time_entries"
PROJECTS = f"/projects/business/{BUSINESS_ID}/projects"


def expected_started_at(day: str) -> str:
    """09:00 local on `day`, expressed as UTC ISO-8601 with milliseconds."""
    year, month, dom = (int(part) for part in day.split("-"))
    local = datetime(year, month, dom, 9, 0).astimezone()
    return local.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def expected_local_midnight(day: str, plus_days: int) -> str:
    """Local midnight `plus_days` after `day`, as UTC ISO-8601 with milliseconds."""
    year, month, dom = (int(part) for part in day.split("-"))
    local = datetime(year, month, dom).astimezone() + timedelta(days=plus_days)
    return local.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class FakeFreshBooks:
    """Minimal stand-in for the time tracking and projects services."""

    def __init__(self) -> None:
        self.entries: dict[int, dict] = {}
        self.next_id = 5000
        # (method, path, json body, query params)
        self.requests: list[tuple[str, str, dict | None, dict[str, str]]] = []

    def seed(self, entry_id: int, **fields) -> dict:
        self.entries[entry_id] = {"id": entry_id, **fields}
        return self.entries[entry_id]

    def calls(self, method: str) -> list[tuple[str, str, dict | None, dict[str, str]]]:
        return [call for call in self.requests if call[0] == method]

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        method, path = request.method, request.url.path
        body = json.loads(request.content) if request.content else None
        self.requests.append((method, path, body, dict(request.url.params)))

        assert request.headers["Authorization"] == "Bearer AT"
        if method == "GET":
            # FreshBooks rejects GETs to these services that carry a Content-Type.
            assert "content-type" not in request.headers

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
                            "project_type": "hourly_rate",
                            "rate": "150",
                        }
                    ],
                    "meta": {"page": 1, "pages": 1, "total": 1},
                },
            )

        if path == TIME_ENTRIES:
            if method == "POST":
                payload = body["time_entry"]
                entry_id = self.next_id
                self.next_id += 1
                self.entries[entry_id] = {"id": entry_id, **payload}
                return httpx.Response(200, json={"time_entry": self.entries[entry_id]})
            if method == "GET":
                return httpx.Response(
                    200,
                    json={
                        "time_entries": list(self.entries.values()),
                        "meta": {"page": 1, "pages": 1, "total": len(self.entries)},
                    },
                )

        if path.startswith(TIME_ENTRIES + "/"):
            entry_id = int(path.rsplit("/", 1)[1])
            if method == "GET":
                if entry_id not in self.entries:
                    return httpx.Response(404, json={"message": "Time Entry not found."})
                return httpx.Response(200, json={"time_entry": self.entries[entry_id]})
            if method == "PUT":
                self.entries[entry_id].update(body["time_entry"])
                return httpx.Response(200, json={"time_entry": self.entries[entry_id]})
            if method == "DELETE":
                self.entries.pop(entry_id, None)
                return httpx.Response(204)

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
            "account_id": "acct123",
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


def log(minutes: float = 90, note: str = "Design review", **extra) -> dict:
    entry = {"date": DAY, "minutes": minutes, "label": "acme", "note": note}
    entry.update(extra)
    return server.log_time([entry])


def test_first_call_creates_the_entry(fake):
    result = log()

    assert result["summary"] == {"created": 1, "updated": 0, "unchanged": 0, "failed": 0}
    assert result["results"][0]["action"] == "created"
    entry_id = result["results"][0]["time_entry_id"]

    posts = fake.calls("POST")
    assert len(posts) == 1
    payload = posts[0][2]["time_entry"]
    assert payload == {
        "is_logged": True,
        "duration": 5400,
        "note": "Design review",
        "started_at": expected_started_at(DAY),
        "client_id": CLIENT_ID,
        "project_id": PROJECT_ID,
        "identity_id": IDENTITY_ID,
        "billable": True,
    }
    assert payload["started_at"].endswith("Z")
    assert store.load_ledger() == {f"{PROJECT_ID}:{DAY}": entry_id}


def test_repeat_of_identical_entry_is_unchanged_and_writes_nothing(fake):
    first = log()
    fake.requests.clear()

    second = log()

    assert second["summary"] == {"created": 0, "updated": 0, "unchanged": 1, "failed": 0}
    assert second["results"][0]["action"] == "unchanged"
    assert second["results"][0]["time_entry_id"] == first["results"][0]["time_entry_id"]
    assert fake.calls("PUT") == []
    assert fake.calls("POST") == []


def test_changed_minutes_update_the_same_entry(fake):
    entry_id = log()["results"][0]["time_entry_id"]
    fake.requests.clear()

    result = log(minutes=120, note="Design review + notes")

    assert result["summary"] == {"created": 0, "updated": 1, "unchanged": 0, "failed": 0}
    assert result["results"][0]["time_entry_id"] == entry_id
    assert fake.calls("POST") == []

    puts = fake.calls("PUT")
    assert len(puts) == 1
    assert puts[0][1] == f"{TIME_ENTRIES}/{entry_id}"
    assert puts[0][2]["time_entry"]["duration"] == 7200
    assert puts[0][2]["time_entry"]["note"] == "Design review + notes"
    assert fake.entries[entry_id]["duration"] == 7200
    assert store.load_ledger() == {f"{PROJECT_ID}:{DAY}": entry_id}


def test_hand_entered_entry_on_the_same_day_is_never_touched(fake):
    hand_entered = fake.seed(
        999,
        duration=1800,
        note="typed straight into FreshBooks",
        project_id=PROJECT_ID,
        client_id=CLIENT_ID,
        started_at=expected_started_at(DAY),
    )
    snapshot = dict(hand_entered)

    result = log()

    assert result["results"][0]["action"] == "created"
    assert result["results"][0]["time_entry_id"] != 999
    assert fake.entries[999] == snapshot
    assert not any(str(call[1]).endswith("/999") for call in fake.requests if call[0] != "GET")


def test_delete_refuses_an_entry_not_in_the_ledger(fake):
    fake.seed(999, duration=1800, note="hand entered", project_id=PROJECT_ID)

    result = server.delete_time_entry(999)

    assert "error" in result
    assert "999" in result["error"]
    assert fake.calls("DELETE") == []
    assert 999 in fake.entries


def test_delete_removes_a_ledger_owned_entry(fake):
    entry_id = log()["results"][0]["time_entry_id"]

    result = server.delete_time_entry(entry_id)

    assert result == {"deleted": True, "time_entry_id": entry_id}
    assert entry_id not in fake.entries
    assert store.load_ledger() == {}


def test_entry_deleted_in_freshbooks_is_recreated(fake):
    entry_id = log()["results"][0]["time_entry_id"]
    fake.entries.pop(entry_id)  # deleted in the FreshBooks UI
    fake.requests.clear()

    result = log()

    assert result["results"][0]["action"] == "created"
    new_id = result["results"][0]["time_entry_id"]
    assert new_id != entry_id
    assert store.load_ledger() == {f"{PROJECT_ID}:{DAY}": new_id}


def test_unknown_label_fails_only_its_own_entry(fake):
    result = server.log_time(
        [
            {"date": DAY, "minutes": 30, "label": "nope", "note": "x"},
            {"date": DAY, "minutes": 60, "label": "acme", "note": "y"},
        ]
    )

    assert result["summary"] == {"created": 1, "updated": 0, "unchanged": 0, "failed": 1}
    assert result["results"][0]["action"] == "failed"
    assert "nope" in result["results"][0]["error"]
    assert result["results"][1]["action"] == "created"


def test_list_time_entries_flags_ledger_ownership_and_local_dates(fake):
    hand_entered = fake.seed(
        999,
        duration=1800,
        note="hand entered",
        project_id=PROJECT_ID,
        client_id=CLIENT_ID,
        started_at=expected_started_at(DAY),
    )
    ours = log()["results"][0]["time_entry_id"]

    listed = server.list_time_entries(DAY, DAY)["time_entries"]
    by_id = {entry["id"]: entry for entry in listed}

    assert by_id[ours]["owned_by_ledger"] is True
    assert by_id[999]["owned_by_ledger"] is False
    assert by_id[ours]["minutes"] == 90
    assert by_id[999]["date"] == DAY  # started_at came back as UTC, reported local
    assert hand_entered["duration"] == 1800

    # The window must cover the whole local day, not stop at its first instant.
    params = next(call for call in fake.calls("GET") if call[1] == TIME_ENTRIES)[3]
    assert params["started_from"] == expected_local_midnight(DAY, 0)
    assert params["started_to"] == expected_local_midnight(DAY, 1)


def test_raw_project_id_resolves_client_id_from_projects(fake):
    result = server.log_time(
        [{"date": DAY, "minutes": 45, "project_id": PROJECT_ID, "note": "z", "billable": False}]
    )

    assert result["results"][0]["action"] == "created"
    payload = fake.calls("POST")[0][2]["time_entry"]
    assert payload["client_id"] == CLIENT_ID
    assert payload["billable"] is False
