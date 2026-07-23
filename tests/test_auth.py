"""Hermetic tests for the offline OAuth 2.1 token verifier.

No network and no external AS dependency: an RS256 keypair is generated in-process,
a JWKS is built from its public key, and the verifier's JWKS source is
monkeypatched to return it. Tokens are minted locally with the private key.
"""
import json
import os
import time

# The package __init__ imports server, which builds a ZendeskClient at import
# time (zenpy requires a non-empty token). These dummy creds never leave the
# process — the auth tests never touch the Zendesk API.
os.environ.setdefault("ZENDESK_SUBDOMAIN", "dummy")
os.environ.setdefault("ZENDESK_EMAIL", "dummy@example.com")
os.environ.setdefault("ZENDESK_API_KEY", "dummy")

import socket

import anyio
import httpx
import jwt
import pytest
import uvicorn
from cryptography.hazmat.primitives.asymmetric import rsa
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from zendesk_mcp_server.auth import (
    AuthConfig,
    AuthConfigError,
    OIDCTokenVerifier,
    load_auth_config,
)

ISSUER = "https://iss.example"
AUDIENCE = "http://localhost:8080/mcp"
KID = "test-key-1"

# One keypair for the whole module; a second, unrelated key for bad-signature.
_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwks(key=_KEY, kid=KID):
    """Build a JWKS dict from a public key, as the AS's jwks_uri would serve."""
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update({"kid": kid, "use": "sig", "alg": "RS256"})
    return {"keys": [jwk]}


def _mint(key=_KEY, kid=KID, iss=ISSUER, aud=AUDIENCE, sub="user_123", exp=None, extra=None):
    now = int(time.time())
    payload = {"iss": iss, "sub": sub, "iat": now, "exp": now + 300 if exp is None else exp}
    if aud is not None:
        payload["aud"] = aud
    if extra:
        payload.update(extra)
    return jwt.encode(payload, key, algorithm="RS256", headers={"kid": kid})


def _make_verifier(monkeypatch, *, issuer=ISSUER, audience=AUDIENCE, jwks=None):
    verifier = OIDCTokenVerifier(
        issuer=issuer,
        jwks_uri="https://iss.example/jwks",
        expected_audience=audience,
        resource_url=AUDIENCE,
    )
    served = _jwks() if jwks is None else jwks

    async def _fake_get_jwks():
        return served

    monkeypatch.setattr(verifier, "_get_jwks", _fake_get_jwks)
    return verifier


def test_valid_token_verifies(monkeypatch):
    verifier = _make_verifier(monkeypatch)
    token = _mint(sub="user_123")
    access = anyio.run(verifier.verify_token, token)
    assert access is not None
    assert access.subject == "user_123"
    assert access.claims["iss"] == ISSUER
    assert access.token == token


def test_scopes_and_client_id_mapped(monkeypatch):
    verifier = _make_verifier(monkeypatch)
    token = _mint(extra={"scope": "read tickets", "client_id": "client_abc"})
    access = anyio.run(verifier.verify_token, token)
    assert access is not None
    assert access.scopes == ["read", "tickets"]
    assert access.client_id == "client_abc"


# name -> factory producing a token that MUST be rejected (verify_token -> None).
_REJECTIONS = {
    "expired": lambda: _mint(exp=int(time.time()) - 60),
    "wrong_issuer": lambda: _mint(iss="https://evil.example"),
    "wrong_audience": lambda: _mint(aud="https://not-our-resource.example"),
    "bad_signature": lambda: _mint(key=_OTHER_KEY),  # signed by a key not in the JWKS
    "malformed": lambda: "not.a.valid.jwt",
    "unknown_kid": lambda: _mint(kid="no-such-kid"),
}


@pytest.mark.parametrize("case", sorted(_REJECTIONS))
def test_invalid_tokens_rejected(monkeypatch, case):
    verifier = _make_verifier(monkeypatch)
    token = _REJECTIONS[case]()
    assert anyio.run(verifier.verify_token, token) is None


def test_audience_not_checked_when_unset(monkeypatch):
    # With expected_audience=None the verifier must accept a token whose aud is
    # NOT our resource url (some ASes set aud to the client id / org instead).
    verifier = _make_verifier(monkeypatch, audience=None)
    token = _mint(aud="client_xyz")
    access = anyio.run(verifier.verify_token, token)
    assert access is not None and access.subject == "user_123"


_AUTH_ENV_KEYS = (
    "MCP_AUTH_DISABLED",
    "MCP_AUTH_ISSUER",
    "MCP_RESOURCE_URL",
    "MCP_EXPECTED_AUDIENCE",
    "MCP_REQUIRED_SCOPES",
    "MCP_ALLOW_ANY_AUDIENCE",
)


@pytest.fixture
def clean_auth_env(monkeypatch):
    for key in _AUTH_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


def test_load_auth_config_raises_when_unconfigured(clean_auth_env):
    # FAIL-CLOSED: no issuer and not explicitly disabled -> refuse to boot.
    with pytest.raises(AuthConfigError):
        load_auth_config()


def test_load_auth_config_raises_when_issuer_empty(clean_auth_env):
    # An explicitly-empty issuer must be treated as unset, not silently unauth'd.
    clean_auth_env.setenv("MCP_AUTH_ISSUER", "")
    with pytest.raises(AuthConfigError):
        load_auth_config()


def test_load_auth_config_none_when_disabled(clean_auth_env):
    # Explicit opt-out is the ONLY way to get the unauthenticated path.
    clean_auth_env.setenv("MCP_AUTH_DISABLED", "1")
    assert load_auth_config() is None


def test_load_auth_config_empty_resource_url_falls_back(clean_auth_env):
    # Empty MCP_RESOURCE_URL must fall back to the default, not stay "" (which
    # would crash build_resource_metadata_url).
    clean_auth_env.setenv("MCP_AUTH_ISSUER", ISSUER)
    clean_auth_env.setenv("MCP_RESOURCE_URL", "")
    clean_auth_env.setenv("MCP_ALLOW_ANY_AUDIENCE", "1")  # keep this test focused on resource_url
    cfg = load_auth_config()
    assert cfg is not None
    assert cfg.resource_url == "http://localhost:8080/mcp"


def test_load_auth_config_on_by_default_with_issuer(clean_auth_env):
    clean_auth_env.setenv("MCP_AUTH_ISSUER", ISSUER)
    clean_auth_env.setenv("MCP_ALLOW_ANY_AUDIENCE", "1")  # else the audience is required
    cfg = load_auth_config()
    assert cfg is not None
    assert cfg.issuer == ISSUER
    assert cfg.resource_url == "http://localhost:8080/mcp"  # default
    assert cfg.required_scopes == []  # [] never None — RequireAuthMiddleware iterates it
    assert cfg.expected_audience is None  # not enforced (explicit opt-out)


def test_load_auth_config_requires_audience(clean_auth_env):
    # FAIL-CLOSED: auth on, issuer set, but no audience and no opt-out -> raise.
    clean_auth_env.setenv("MCP_AUTH_ISSUER", ISSUER)
    with pytest.raises(AuthConfigError):
        load_auth_config()


def test_load_auth_config_audience_opt_out(clean_auth_env):
    clean_auth_env.setenv("MCP_AUTH_ISSUER", ISSUER)
    clean_auth_env.setenv("MCP_ALLOW_ANY_AUDIENCE", "1")
    cfg = load_auth_config()
    assert cfg is not None and cfg.expected_audience is None


def test_load_auth_config_parses_scopes_and_audience(clean_auth_env):
    clean_auth_env.setenv("MCP_AUTH_ISSUER", ISSUER)
    clean_auth_env.setenv("MCP_REQUIRED_SCOPES", "read write")
    clean_auth_env.setenv("MCP_EXPECTED_AUDIENCE", AUDIENCE)
    cfg = load_auth_config()
    assert cfg is not None
    assert cfg.required_scopes == ["read", "write"]
    assert cfg.expected_audience == AUDIENCE


# --- HTTP wiring tests (auth enabled end-to-end over a real uvicorn) ---------


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _test_cfg(port: int) -> AuthConfig:
    """Auth config whose resource_url matches the actual serving origin."""
    return AuthConfig(
        issuer=ISSUER,
        resource_url=f"http://127.0.0.1:{port}/mcp",
        required_scopes=[],
        expected_audience=AUDIENCE,
    )


def _drive(app, port, client_fn):
    """Serve `app` on `port` with uvicorn and run async `client_fn(port)`."""

    async def _main():
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        server = uvicorn.Server(config)
        result = {}
        async with anyio.create_task_group() as tg:
            tg.start_soon(server.serve)
            try:
                with anyio.fail_after(20):
                    while not server.started:
                        await anyio.sleep(0.05)
                    result["value"] = await client_fn(port)
            finally:
                server.should_exit = True
        return result["value"]

    return anyio.run(_main)


def test_unauthenticated_request_rejected():
    from zendesk_mcp_server.server import build_app

    port = _free_port()
    app = build_app(auth_config=_test_cfg(port))

    async def client(port):
        async with httpx.AsyncClient() as c:
            return await c.post(
                f"http://127.0.0.1:{port}/mcp",
                json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                headers={
                    "Accept": "application/json, text/event-stream",
                    "Content-Type": "application/json",
                },
            )

    resp = _drive(app, port, client)
    assert resp.status_code == 401
    assert "resource_metadata" in resp.headers.get("www-authenticate", "").lower()


def test_authenticated_session_lists_tools(monkeypatch):
    from zendesk_mcp_server.server import build_app

    # Point every verifier the app builds at our in-process stub JWKS (no network).
    served = _jwks()

    async def _fake_get_jwks(self):
        return served

    monkeypatch.setattr(OIDCTokenVerifier, "_get_jwks", _fake_get_jwks)

    port = _free_port()
    app = build_app(auth_config=_test_cfg(port))
    token = _mint(aud=AUDIENCE)

    async def client(port):
        url = f"http://127.0.0.1:{port}/mcp"
        # streamable_http_client takes no auth/headers kwarg; inject via the client.
        http_client = httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"})
        async with streamable_http_client(url, http_client=http_client) as (r, w, _):
            async with ClientSession(r, w) as session:
                await session.initialize()
                result = await session.list_tools()
                return [t.name for t in result.tools]

    names = set(_drive(app, port, client))
    # Representative read tools plus the write tools all reach an authenticated
    # client — the auth layer gates access, it does not filter the tool set.
    assert "search" in names
    assert "get_ticket_counts_by_status" in names
    assert {"create_ticket", "create_ticket_comment", "update_ticket"}.issubset(names)


def test_protected_resource_metadata_served():
    from zendesk_mcp_server.server import build_app

    port = _free_port()
    cfg = _test_cfg(port)
    app = build_app(auth_config=cfg)

    async def client(port):
        async with httpx.AsyncClient() as c:
            # Resource-SUFFIXED well-known path (gotcha 1); the bare path 404s.
            return await c.get(
                f"http://127.0.0.1:{port}/.well-known/oauth-protected-resource/mcp"
            )

    resp = _drive(app, port, client)
    assert resp.status_code == 200
    data = resp.json()
    # authorization_servers is coerced through AnyHttpUrl (trailing slash added),
    # so compare normalized, not raw (gotcha in Task 2 Step 6).
    assert data["resource"].rstrip("/") == cfg.resource_url.rstrip("/")
    servers = [s.rstrip("/") for s in data["authorization_servers"]]
    assert ISSUER.rstrip("/") in servers
