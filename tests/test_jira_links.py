"""Tests for the Zendesk<->Jira issue link tools."""
# REQUIRED, and must come first: importing this package constructs a
# ZendeskClient at import time (server.py), and zenpy raises
# "password, token or oauth_token are required!" on empty credentials.
# Every other test module does the same — see tests/test_auth.py.
import os
os.environ.setdefault("ZENDESK_SUBDOMAIN", "dummy")
os.environ.setdefault("ZENDESK_EMAIL", "dummy@example.com")
os.environ.setdefault("ZENDESK_API_KEY", "dummy")

import io
import json
import urllib.error
import urllib.parse

import pytest

from zendesk_mcp_server.zendesk_client import ZendeskClient

URLOPEN = "zendesk_mcp_server.zendesk_client.urllib.request.urlopen"


class _FakeResponse:
    """Stands in for the context manager urllib.request.urlopen returns."""

    def __init__(self, payload):
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _link(link_id, ticket_id="12345", issue_id="10001", issue_key="ABC-123"):
    return {
        "id": link_id,
        "ticket_id": ticket_id,
        "issue_id": issue_id,
        "issue_key": issue_key,
        "created_at": "2026-01-01T00:00:00.000Z",
        "updated_at": "2026-01-01T00:00:00.000Z",
    }


def _query(url):
    """The query string of a request URL, as a flat dict."""
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))


@pytest.fixture
def client(monkeypatch):
    """A ZendeskClient with zenpy stubbed out (no network, no credentials)."""
    monkeypatch.setattr(
        "zendesk_mcp_server.zendesk_client.Zenpy", lambda **kwargs: object()
    )
    c = ZendeskClient(subdomain="example", email="a@b.c", token="tok")
    monkeypatch.setattr(c, "get_ticket", lambda tid: {"id": tid})
    return c


def test_list_jira_links_reads_v2_endpoint_filtered_by_ticket(client, monkeypatch):
    requests = []

    def fake_urlopen(req):
        requests.append(req)
        return _FakeResponse({"links": [_link(555)], "total": 1,
                              "meta": {"has_more": False}})

    monkeypatch.setattr(URLOPEN, fake_urlopen)

    result = client.list_jira_links(12345)

    assert len(requests) == 1
    url = requests[0].full_url
    assert url.startswith("https://example.zendesk.com/api/v2/jira/links?")
    assert _query(url) == {"filter[ticket_id]": "12345"}
    assert requests[0].get_header("Authorization").startswith("Basic ")
    assert result == {
        "ticket_id": 12345,
        "total": 1,
        "links": [{
            "link_id": 555, "ticket_id": "12345", "issue_id": "10001",
            "issue_key": "ABC-123", "created_at": "2026-01-01T00:00:00.000Z",
            "updated_at": "2026-01-01T00:00:00.000Z",
        }],
    }


def test_list_jira_links_follows_pages_keeping_the_filter(client, monkeypatch):
    pages = [
        {"links": [_link(100)], "total": 2,
         "meta": {"has_more": True, "after_cursor": "https://elsewhere.example/next"}},
        {"links": [_link(200, issue_key="ABC-200")], "total": 2,
         "meta": {"has_more": False}},
    ]
    urls = []

    def fake_urlopen(req):
        urls.append(req.full_url)
        return _FakeResponse(pages[len(urls) - 1])

    monkeypatch.setattr(URLOPEN, fake_urlopen)

    result = client.list_jira_links(12345)

    # The second request is built here from the last link id, never from the
    # response's after_cursor URL, and it keeps the ticket filter.
    assert [urllib.parse.urlsplit(u).netloc for u in urls] == ["example.zendesk.com"] * 2
    assert _query(urls[1]) == {"filter[ticket_id]": "12345", "page[after]": "100"}
    assert result["total"] == 2
    assert [l["issue_key"] for l in result["links"]] == ["ABC-123", "ABC-200"]


def test_list_jira_links_rejects_unknown_ticket(client, monkeypatch):
    """The endpoint returns [] for a nonexistent ticket; the tool must not."""
    def missing(tid):
        raise Exception("Failed to get ticket: RecordNotFoundException")

    def explode(req):
        raise AssertionError("must not list links for a ticket that does not exist")

    monkeypatch.setattr(client, "get_ticket", missing)
    monkeypatch.setattr(URLOPEN, explode)

    with pytest.raises(Exception, match="Could not confirm Zendesk ticket 999999 exists"):
        client.list_jira_links(999999)


def test_list_jira_links_reports_http_errors(client, monkeypatch):
    def forbidden(req):
        raise urllib.error.HTTPError(
            req.full_url, 403, "Forbidden", {}, io.BytesIO(b'{"error":"Forbidden"}')
        )

    monkeypatch.setattr(URLOPEN, forbidden)

    with pytest.raises(Exception, match="Failed to list Jira links for ticket 12345: HTTP 403"):
        client.list_jira_links(12345)


def test_get_jira_links_is_registered_and_dispatches(monkeypatch):
    import anyio
    from zendesk_mcp_server import server as srv

    names = {t.name for t in anyio.run(srv.handle_list_tools)}
    assert "get_jira_links" in names

    monkeypatch.setattr(srv.zendesk_client, "list_jira_links",
                        lambda ticket_id: {"ticket_id": ticket_id, "total": 0, "links": []})

    out = srv._dispatch_tool("get_jira_links", {"ticket_id": 12345})
    assert json.loads(out[0].text)["total"] == 0


def test_dispatch_requires_arguments():
    from zendesk_mcp_server import server as srv
    with pytest.raises(ValueError, match="Missing required argument"):
        srv._dispatch_tool("get_jira_links", {})
