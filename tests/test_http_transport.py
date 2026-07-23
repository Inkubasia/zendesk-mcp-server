"""Smoke test for the streamable-HTTP transport.

Drives one full MCP session (initialize -> tools/list) over HTTP against the
ASGI app built by ``build_app()``, served by a real uvicorn instance on an
ephemeral port. Asserts the HTTP transport exposes the server's full MCP tool
set (parity with stdio) — both read and write tools reach the client.
"""
import os
import socket

# The server module constructs a ZendeskClient at import time, and zenpy
# requires a non-empty token. These dummy creds never leave the process:
# initialize + tools/list do not touch the Zendesk API.
os.environ.setdefault("ZENDESK_SUBDOMAIN", "dummy")
os.environ.setdefault("ZENDESK_EMAIL", "dummy@example.com")
os.environ.setdefault("ZENDESK_API_KEY", "dummy")

import anyio
import uvicorn
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _initialize_and_list_tools(port: int) -> list[str]:
    url = f"http://127.0.0.1:{port}/mcp"
    async with streamable_http_client(url) as (read, write, _get_session_id):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.list_tools()
            return [t.name for t in result.tools]


async def _drive() -> list[str]:
    from zendesk_mcp_server.server import build_app

    # Auth is fail-closed by default; pass None explicitly to exercise the
    # unauthenticated transport.
    app = build_app(auth_config=None)
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)

    names: list[str] = []
    async with anyio.create_task_group() as tg:
        tg.start_soon(server.serve)
        try:
            with anyio.fail_after(15):
                while not server.started:
                    await anyio.sleep(0.05)
                names = await _initialize_and_list_tools(port)
        finally:
            server.should_exit = True
    return names


def test_http_app_lists_tools():
    names = set(anyio.run(_drive))

    # (a) the session completed and returned a non-empty tool list
    assert names, "tools/list returned no tools"
    # (b) representative read tools are present
    assert "search" in names
    assert "get_ticket_counts_by_status" in names
    # (c) write tools reach the client too — HTTP mirrors the stdio tool set
    assert {"create_ticket", "create_ticket_comment", "update_ticket"}.issubset(names)
