"""Tests for the Graph client and inbox indexing, using a fake HTTP transport."""

import json

import httpx
import pytest

from mail_sorter import graph
from mail_sorter.db import Email, sender_stats
from mail_sorter.graph import MESSAGE_FIELDS, GraphClient, GraphError, NotLoggedInError
from mail_sorter.processor import index_inbox, parse_message


def make_raw(msg_id: str, address: str = "news@riotgames.com", **extra) -> dict:
    raw = {
        "id": msg_id,
        "subject": f"Subject {msg_id}",
        "from": {"emailAddress": {"name": "Riot Games", "address": address}},
        "receivedDateTime": "2025-03-01T10:00:00Z",
        "isRead": False,
        "hasAttachments": False,
        "importance": "normal",
        "singleValueExtendedProperties": [{"id": "Integer 0xe08", "value": "2048"}],
    }
    raw.update(extra)
    return raw


def client_for(handler) -> GraphClient:
    return GraphClient(token_provider=lambda: "token", http=httpx.Client(transport=handler))


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(graph, "sleep", lambda _s: None)


def test_not_logged_in_raises():
    client = GraphClient(token_provider=lambda: None, http=httpx.Client())
    with pytest.raises(NotLoggedInError):
        client.inbox_count()


def test_iter_messages_follows_pagination_and_selects_metadata_only():
    seen_params = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_params.append(dict(request.url.params))
        if "skip" in str(request.url):
            return httpx.Response(200, json={"value": [make_raw("b")]})
        return httpx.Response(
            200,
            json={
                "value": [make_raw("a")],
                "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/next?skip=1",
            },
        )

    messages = list(client_for(httpx.MockTransport(handler)).iter_messages())
    assert [m["id"] for m in messages] == ["a", "b"]
    assert seen_params[0]["$select"] == MESSAGE_FIELDS
    assert "body" not in MESSAGE_FIELDS.lower()


def test_retries_on_429_then_succeeds():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429, headers={"Retry-After": "1"})
        return httpx.Response(200, json={"totalItemCount": 42})

    assert client_for(httpx.MockTransport(handler)).inbox_count() == 42
    assert calls["n"] == 3


def test_http_error_raises_graph_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": {"message": "Access denied"}})

    with pytest.raises(GraphError, match="Access denied"):
        client_for(httpx.MockTransport(handler)).inbox_count()


def test_ensure_folder_creates_missing_levels():
    created = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/me/mailFolders"):
            return httpx.Response(200, json={"value": [{"id": "jeux", "displayName": "Jeux"}]})
        if request.method == "GET" and path.endswith("/jeux/childFolders"):
            return httpx.Response(200, json={"value": []})
        if request.method == "POST":
            created.append((path, json.loads(request.content)["displayName"]))
            return httpx.Response(201, json={"id": "riot"})
        return httpx.Response(404)

    client = client_for(httpx.MockTransport(handler))
    assert client.ensure_folder("jeux/Riot") == "riot"
    assert created == [("/v1.0/me/mailFolders/jeux/childFolders", "Riot")]
    # second call is served from cache
    assert client.ensure_folder("Jeux/riot") == "riot"
    assert len(created) == 1


def test_move_messages_batches_and_retries_throttled_items():
    batches = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        batches.append(len(body["requests"]))
        responses = []
        for req in body["requests"]:
            msg_id = req["url"].split("/")[3]
            # the first time, message m1 is throttled
            if msg_id == "m1" and len(batches) == 1:
                responses.append({"id": req["id"], "status": 429, "headers": {"Retry-After": "1"}})
            elif msg_id == "bad":
                responses.append({"id": req["id"], "status": 404, "body": {}})
            else:
                responses.append({"id": req["id"], "status": 201, "body": {"id": f"new_{msg_id}"}})
        return httpx.Response(200, json={"responses": responses})

    ids = [f"m{i}" for i in range(25)] + ["bad"]
    result = client_for(httpx.MockTransport(handler)).move_messages(ids, "deleteditems")

    assert result["m1"] == "new_m1"
    assert result["m24"] == "new_m24"
    assert result["bad"] is None
    assert batches == [20, 1, 6]


def test_parse_message_maps_fields():
    data = parse_message(make_raw("x", address="News@RiotGames.com", importance="high"))
    assert data["sender_email"] == "news@riotgames.com"
    assert data["size_bytes"] == 2048
    assert data["importance"] == "high"
    assert data["received_at"].year == 2025
    assert data["received_at"].tzinfo is None


def test_parse_message_without_sender():
    data = parse_message({"id": "draft", "from": None})
    assert data["sender_email"] == ""
    assert data["received_at"] is None


class FakeClient:
    def __init__(self, messages):
        self.messages = messages

    def iter_messages(self, folder="inbox"):
        yield from self.messages


def test_index_inbox_adds_updates_and_marks_gone(db_session):
    stats = index_inbox(FakeClient([make_raw("a"), make_raw("b")]), db_session)
    assert (stats.added, stats.updated, stats.gone) == (2, 0, 0)

    db_session.query(Email).filter_by(message_id="a").one().category = "PROMO"
    stats = index_inbox(FakeClient([make_raw("a", isRead=True), make_raw("c")]), db_session)
    assert (stats.added, stats.updated, stats.gone) == (1, 1, 1)

    a = db_session.query(Email).filter_by(message_id="a").one()
    assert a.is_read is True
    assert a.category == "PROMO"
    assert db_session.query(Email).filter_by(message_id="b").one().status == "gone"


def test_index_inbox_reactivates_email_moved_back(db_session):
    index_inbox(FakeClient([make_raw("a")]), db_session)
    email = db_session.query(Email).filter_by(message_id="a").one()
    email.status = "moved"
    email.action_taken = "moved:Riot"

    index_inbox(FakeClient([make_raw("a")]), db_session)
    assert email.status == "pending"
    assert email.action_taken is None


def test_sender_stats_groups_by_domain(db_session):
    raws = [make_raw("a"), make_raw("b", address="support@riotgames.com"), make_raw("c", "x@y.fr")]
    index_inbox(FakeClient(raws), db_session)

    by_domain = sender_stats(db_session, "domain")
    assert by_domain[0]["sender"] == "riotgames.com"
    assert by_domain[0]["count"] == 2
    assert by_domain[0]["size_bytes"] == 4096

    by_email = sender_stats(db_session, "email")
    assert len(by_email) == 3
