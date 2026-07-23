"""Offline OAuth 2.1 token verification for the streamable-HTTP MCP endpoint.

The MCP server acts purely as an OAuth 2.1 *Resource Server*. An external
Authorization Server (any OAuth 2.1 / OIDC provider — login, dynamic client
registration, PKCE, token issuance) issues the tokens. Verification is fully
offline: a JWT's signature is checked against the AS's published JWKS and its
issuer/expiry (and optionally audience) are validated. No per-request network
call after the JWKS is cached, and no AS API key or secret on the server.

`load_auth_config()` reads env into an `AuthConfig`, returning `None` when auth is
disabled or unconfigured (in which case `build_app()` serves the unauthenticated
app). `OIDCTokenVerifier.verify_token()` turns a bearer token into a validated
`AccessToken` or `None`.
"""
import json
import logging
import os
import time
from dataclasses import dataclass, field

import httpx
import jwt
from mcp.server.auth.provider import AccessToken, TokenVerifier

logger = logging.getLogger("zendesk-mcp-server.auth")

# Default JWKS cache lifetime. Authorization Servers rotate signing keys rarely;
# a short TTL keeps rotation lag bounded without a network round-trip per request.
JWKS_TTL_SECONDS = 300


@dataclass
class AuthConfig:
    """Resolved auth configuration. Built by ``load_auth_config()`` from env."""

    issuer: str
    resource_url: str
    required_scopes: list[str] = field(default_factory=list)
    # None => the verifier does NOT check `aud` (some ASes may not mint the RFC 8707
    # resource into `aud`). Set MCP_EXPECTED_AUDIENCE once the real claim is known.
    expected_audience: str | None = None
    # None => discover jwks_uri from the AS metadata document at verify time.
    jwks_uri: str | None = None
    resource_name: str = "Zendesk MCP"


class OIDCTokenVerifier(TokenVerifier):
    """Verify RS256 access tokens offline against the AS's cached JWKS."""

    def __init__(
        self,
        issuer: str,
        *,
        resource_url: str,
        jwks_uri: str | None = None,
        expected_audience: str | None = None,
        jwks_ttl: int = JWKS_TTL_SECONDS,
    ) -> None:
        self.issuer = issuer.rstrip("/")
        self.resource_url = resource_url
        self._jwks_uri = jwks_uri
        self.expected_audience = expected_audience
        self._jwks_ttl = jwks_ttl
        self._jwks_cache: dict | None = None
        self._jwks_expiry: float = 0.0

    async def _discover_jwks_uri(self, client: httpx.AsyncClient) -> str:
        """Resolve jwks_uri from the AS metadata document (cached on the instance)."""
        if self._jwks_uri:
            return self._jwks_uri
        # issuer is already right-stripped of '/', avoiding '//.well-known'.
        meta_url = f"{self.issuer}/.well-known/oauth-authorization-server"
        resp = await client.get(meta_url)
        resp.raise_for_status()
        jwks_uri = resp.json()["jwks_uri"]
        self._jwks_uri = jwks_uri
        return jwks_uri

    async def _get_jwks(self) -> dict:
        """Return the JWKS, fetching + caching with a short TTL. Monkeypatched in tests."""
        now = time.time()
        if self._jwks_cache is not None and now < self._jwks_expiry:
            return self._jwks_cache
        async with httpx.AsyncClient(timeout=10.0) as client:
            jwks_uri = await self._discover_jwks_uri(client)
            resp = await client.get(jwks_uri)
            resp.raise_for_status()
            jwks = resp.json()
        self._jwks_cache = jwks
        self._jwks_expiry = now + self._jwks_ttl
        return jwks

    @staticmethod
    def _select_key(jwks: dict, kid: str | None) -> dict | None:
        keys = jwks.get("keys", [])
        for key in keys:
            if key.get("kid") == kid:
                return key
        # No kid in the token header: fall back only when the JWKS has exactly one key.
        if kid is None and len(keys) == 1:
            return keys[0]
        return None

    async def verify_token(self, token: str) -> AccessToken | None:
        """Validate a bearer token; return an AccessToken or None on any failure."""
        try:
            header = jwt.get_unverified_header(token)
            jwks = await self._get_jwks()
            key = self._select_key(jwks, header.get("kid"))
            if key is None:
                return None
            public_key = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(key))

            # Always require exp + iss. Verify aud ONLY when an expected audience
            # is configured — the AS may not put the RFC 8707 resource in `aud`.
            verify_aud = self.expected_audience is not None
            require = ["exp", "iss", "aud"] if verify_aud else ["exp", "iss"]
            claims = jwt.decode(
                token,
                public_key,
                algorithms=["RS256"],
                issuer=self.issuer,
                audience=self.expected_audience if verify_aud else None,
                options={"require": require, "verify_aud": verify_aud},
            )
        except Exception as exc:  # jwt.* errors, malformed header, JWKS shape, etc.
            logger.debug("token verification failed: %s", exc)
            return None

        scope = claims.get("scope", "")
        scopes = scope.split() if isinstance(scope, str) and scope else []
        return AccessToken(
            token=token,
            client_id=claims.get("client_id") or claims.get("azp") or "",
            scopes=scopes,
            expires_at=claims.get("exp"),
            resource=self.resource_url,
            subject=claims.get("sub"),
            claims=claims,
        )


class AuthConfigError(RuntimeError):
    """Raised when auth is required but misconfigured — the server must not boot."""


def load_auth_config() -> AuthConfig | None:
    """Build AuthConfig from env. FAIL-CLOSED.

    Returns None (=> unauthenticated behaviour) ONLY when auth is explicitly
    disabled via MCP_AUTH_DISABLED=1. Otherwise auth is required: if
    MCP_AUTH_ISSUER is unset/empty this RAISES AuthConfigError rather than
    silently serving unauthenticated — a missing issuer on a public service must
    fail to boot, not expose every tool. Empty env strings are treated as unset.
    required_scopes is [] (never None) when MCP_REQUIRED_SCOPES is unset —
    RequireAuthMiddleware iterates it.
    """
    if os.getenv("MCP_AUTH_DISABLED") == "1":
        logger.warning(
            "MCP_AUTH_DISABLED=1 — /mcp is served UNAUTHENTICATED. Dev/local only; "
            "never set this on a publicly reachable deployment."
        )
        return None

    issuer = (os.getenv("MCP_AUTH_ISSUER") or "").strip()
    if not issuer:
        raise AuthConfigError(
            "Auth is required but MCP_AUTH_ISSUER is unset/empty. "
            "Set it to your OAuth 2.1 authorization server's issuer URL, or set "
            "MCP_AUTH_DISABLED=1 to run unauthenticated (dev/local only)."
        )

    # Treat empty strings as unset so an explicitly-empty env can't override the
    # default with an invalid value (an empty resource_url crashes metadata build).
    resource_url = (os.getenv("MCP_RESOURCE_URL") or "").strip() or "http://localhost:8080/mcp"
    required_scopes = (os.getenv("MCP_REQUIRED_SCOPES") or "").split()  # "" -> []
    expected_audience = (os.getenv("MCP_EXPECTED_AUDIENCE") or "").strip() or None

    # FAIL-CLOSED on audience: an AS may be a shared issuer with open (dynamic)
    # client registration, so without an aud check any valid token minted for
    # *another* resource on the same issuer would be accepted. Require
    # MCP_EXPECTED_AUDIENCE unless the operator explicitly opts out.
    if expected_audience is None and os.getenv("MCP_ALLOW_ANY_AUDIENCE") != "1":
        raise AuthConfigError(
            "Auth is enabled but MCP_EXPECTED_AUDIENCE is unset. Set it to this "
            "server's resource URL (the registered RFC 8707 Resource Indicator) so "
            "tokens minted for other resources on the shared issuer are rejected. "
            "Set MCP_ALLOW_ANY_AUDIENCE=1 to intentionally accept any audience."
        )
    if expected_audience is None:
        logger.warning(
            "MCP_ALLOW_ANY_AUDIENCE=1 — accepting ANY audience from the issuer "
            "(aud NOT enforced; cross-resource tokens will be accepted)."
        )

    logger.info(
        "MCP auth ENABLED (issuer=%s, resource=%s, audience=%s, scopes=%s).",
        issuer, resource_url, expected_audience or "<not enforced>",
        required_scopes or "<none>",
    )
    return AuthConfig(
        issuer=issuer,
        resource_url=resource_url,
        required_scopes=required_scopes,
        expected_audience=expected_audience,
        jwks_uri=None,
    )


def build_verifier(config: AuthConfig) -> OIDCTokenVerifier:
    """Construct a OIDCTokenVerifier from an AuthConfig."""
    return OIDCTokenVerifier(
        issuer=config.issuer,
        resource_url=config.resource_url,
        jwks_uri=config.jwks_uri,
        expected_audience=config.expected_audience,
    )
