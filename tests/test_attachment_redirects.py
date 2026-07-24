"""Attachment fetch must re-validate every redirect hop against the Zendesk
host allowlist (no SSRF) and drop the auth header on cross-host hops (no
credential leak). Uses a fake requests.get — no network."""
import base64
import os

# Package __init__ imports server, which builds a ZendeskClient at import time
# (zenpy requires a non-empty token). Dummy creds; never leave the process.
os.environ.setdefault("ZENDESK_SUBDOMAIN", "dummy")
os.environ.setdefault("ZENDESK_EMAIL", "dummy@example.com")
os.environ.setdefault("ZENDESK_API_KEY", "dummy")

import pytest

from zendesk_mcp_server import zendesk_client as zc

# Valid PNG header so the magic-byte check passes.
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


class FakeResp:
    def __init__(self, status_code, headers=None, body=b""):
        self.status_code = status_code
        self.headers = headers or {}
        self._body = body
        self.closed = False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise zc._requests.HTTPError(str(self.status_code))

    def iter_content(self, chunk_size=65536):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i:i + chunk_size]

    def close(self):
        self.closed = True


def _make_client():
    # subdomain "acme" => this tenant's own host is acme.zendesk.com (the only
    # host the Zendesk credential may be sent to).
    return zc.ZendeskClient(subdomain="acme", email="d@example.com", token="tok")


def _install(monkeypatch, responses):
    calls = []
    it = iter(responses)

    def fake_get(url, headers=None, timeout=None, stream=None, allow_redirects=None):
        calls.append({"url": url, "headers": headers or {}, "allow_redirects": allow_redirects})
        return next(it)

    monkeypatch.setattr(zc._requests, "get", fake_get)
    return calls


def _png_ok():
    return FakeResp(200, {"Content-Type": "image/png", "Content-Length": str(len(PNG))}, PNG)


def test_direct_200_sends_auth(monkeypatch):
    calls = _install(monkeypatch, [_png_ok()])
    out = _make_client().get_ticket_attachment("https://acme.zendesk.com/attachments/1")
    assert out["content_type"] == "image/png"
    assert base64.b64decode(out["data"]) == PNG
    assert "Authorization" in calls[0]["headers"]      # auth sent to the Zendesk host
    assert calls[0]["allow_redirects"] is False         # never auto-follows


def test_redirect_to_cdn_drops_auth(monkeypatch):
    calls = _install(monkeypatch, [
        FakeResp(302, {"Location": "https://cdn.zdusercontent.com/blob"}),
        _png_ok(),
    ])
    out = _make_client().get_ticket_attachment("https://acme.zendesk.com/attachments/1")
    assert out["content_type"] == "image/png"
    assert "Authorization" in calls[0]["headers"]       # hop 1: zendesk.com -> auth
    assert "Authorization" not in calls[1]["headers"]   # hop 2: cross-host CDN -> NO auth
    assert calls[1]["url"] == "https://cdn.zdusercontent.com/blob"


def test_redirect_to_foreign_host_blocked(monkeypatch):
    calls = _install(monkeypatch, [
        FakeResp(302, {"Location": "https://evil.example/steal"}),
        _png_ok(),  # must never be reached
    ])
    with pytest.raises(ValueError, match="non-Zendesk host"):
        _make_client().get_ticket_attachment("https://acme.zendesk.com/attachments/1")
    assert len(calls) == 1  # the foreign host was never requested


def test_initial_foreign_host_blocked_no_request(monkeypatch):
    calls = _install(monkeypatch, [])
    with pytest.raises(ValueError, match="non-Zendesk host"):
        _make_client().get_ticket_attachment("https://evil.example/x")
    assert calls == []  # no request issued at all


def test_too_many_redirects(monkeypatch):
    resps = [FakeResp(302, {"Location": f"https://h{i}.zendesk.com/x"}) for i in range(8)]
    _install(monkeypatch, resps)
    with pytest.raises(ValueError, match="Too many redirects"):
        _make_client().get_ticket_attachment("https://acme.zendesk.com/attachments/1")


def test_open_redirect_via_relative_location_stays_on_host(monkeypatch):
    # A relative Location resolves against the current host (stays in-allowlist).
    calls = _install(monkeypatch, [
        FakeResp(302, {"Location": "/redirected/blob"}),
        _png_ok(),
    ])
    out = _make_client().get_ticket_attachment("https://acme.zendesk.com/attachments/1")
    assert out["content_type"] == "image/png"
    assert calls[1]["url"] == "https://acme.zendesk.com/redirected/blob"
    assert "Authorization" in calls[1]["headers"]  # same host -> auth retained


def test_backslash_parser_differential_blocked(monkeypatch):
    # urllib.parse reads host 'acme.zendesk.com' here, but urllib3/requests
    # CONNECT to evil.com. Validating with the connect-parser must reject it,
    # and no request (with our credential) may be issued.
    calls = _install(monkeypatch, [_png_ok()])  # must never be reached
    with pytest.raises(ValueError, match="non-Zendesk host"):
        _make_client().get_ticket_attachment("https://evil.com\\@acme.zendesk.com/x")
    assert calls == []


def test_foreign_zendesk_tenant_gets_no_credential(monkeypatch):
    # A different Zendesk tenant is still off-limits for the credential: the
    # auth header must only ever reach THIS account's own subdomain.
    calls = _install(monkeypatch, [_png_ok()])  # must never be reached
    with pytest.raises(ValueError, match="foreign host"):
        _make_client().get_ticket_attachment("https://other.zendesk.com/attachments/1")
    assert calls == []


def test_scheme_downgrade_redirect_blocked(monkeypatch):
    # A same-host https->http downgrade must not carry the Basic credential over
    # cleartext; the http hop is refused before any request.
    calls = _install(monkeypatch, [
        FakeResp(302, {"Location": "http://acme.zendesk.com/x"}),
        _png_ok(),  # must never be reached
    ])
    with pytest.raises(ValueError, match="non-https"):
        _make_client().get_ticket_attachment("https://acme.zendesk.com/attachments/1")
    assert len(calls) == 1  # only the initial https hop was issued
