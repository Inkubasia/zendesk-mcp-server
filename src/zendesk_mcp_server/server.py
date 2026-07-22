import asyncio
import base64
import json
import logging
import os
import secrets
import tempfile
import time
from pathlib import Path
from typing import Any, Dict

from cachetools.func import ttl_cache
from dotenv import load_dotenv
from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server
from pydantic import AnyUrl

from zendesk_mcp_server.zendesk_client import ZendeskClient

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger("zendesk-mcp-server")
logger.info("zendesk mcp server started")

load_dotenv()
zendesk_client = ZendeskClient(
    subdomain=os.getenv("ZENDESK_SUBDOMAIN"),
    email=os.getenv("ZENDESK_EMAIL"),
    token=os.getenv("ZENDESK_API_KEY")
)

# Identity advertised to clients over every transport (stdio and HTTP alike);
# both paths derive it from here via server.create_initialization_options().
server = Server("Zendesk", version="0.1.0")

TICKET_ANALYSIS_TEMPLATE = """
You are a helpful Zendesk support analyst. You've been asked to analyze ticket #{ticket_id}.

Please fetch the ticket info and comments to analyze it and provide:
1. A summary of the issue
2. The current status and timeline
3. Key points of interaction

Remember to be professional and focus on actionable insights.
"""

COMMENT_DRAFT_TEMPLATE = """
You are a helpful Zendesk support agent. You need to draft a response to ticket #{ticket_id}.

Please fetch the ticket info, comments and knowledge base to draft a professional and helpful response that:
1. Acknowledges the customer's concern
2. Addresses the specific issues raised
3. Provides clear next steps or ask for specific details need to proceed
4. Maintains a friendly and professional tone
5. Ask for confirmation before commenting on the ticket

The response should be formatted well and ready to be posted as a comment.
"""


@server.list_prompts()
async def handle_list_prompts() -> list[types.Prompt]:
    """List available prompts"""
    return [
        types.Prompt(
            name="analyze-ticket",
            description="Analyze a Zendesk ticket and provide insights",
            arguments=[
                types.PromptArgument(
                    name="ticket_id",
                    description="The ID of the ticket to analyze",
                    required=True,
                )
            ],
        ),
        types.Prompt(
            name="draft-ticket-response",
            description="Draft a professional response to a Zendesk ticket",
            arguments=[
                types.PromptArgument(
                    name="ticket_id",
                    description="The ID of the ticket to respond to",
                    required=True,
                )
            ],
        )
    ]


@server.get_prompt()
async def handle_get_prompt(name: str, arguments: Dict[str, str] | None) -> types.GetPromptResult:
    """Handle prompt requests"""
    if not arguments or "ticket_id" not in arguments:
        raise ValueError("Missing required argument: ticket_id")

    ticket_id = int(arguments["ticket_id"])
    try:
        if name == "analyze-ticket":
            prompt = TICKET_ANALYSIS_TEMPLATE.format(
                ticket_id=ticket_id
            )
            description = f"Analysis prompt for ticket #{ticket_id}"

        elif name == "draft-ticket-response":
            prompt = COMMENT_DRAFT_TEMPLATE.format(
                ticket_id=ticket_id
            )
            description = f"Response draft prompt for ticket #{ticket_id}"

        else:
            raise ValueError(f"Unknown prompt: {name}")

        return types.GetPromptResult(
            description=description,
            messages=[
                types.PromptMessage(
                    role="user",
                    content=types.TextContent(type="text", text=prompt.strip()),
                )
            ],
        )

    except Exception as e:
        logger.error(f"Error generating prompt: {e}")
        raise


@server.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    """List available Zendesk tools"""
    return [
        types.Tool(
            name="fetch_result_chunk",
            description=(
                "Retrieve the next slice of a previously truncated tool result. "
                "When any tool's result exceeds the 1 MB MCP cap, it is spilled to "
                "a temp file and the caller gets an envelope with `truncated: true`, "
                "a `result_token`, a `saved_to` path, and a `next_offset`. Call this "
                "with that `result_token` (or `saved_to` as `path`) and `offset` set "
                "to `next_offset` to read the next chunk; repeat until `next_offset` "
                "is null. Each chunk's `data_base64` is base64 of the raw byte slice "
                "— base64-decode and concatenate the bytes across chunks to rebuild "
                "the full payload."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "result_token": {
                        "type": "string",
                        "description": "The result_token from a truncated result envelope."
                    },
                    "path": {
                        "type": "string",
                        "description": "Alternatively, the saved_to path from the envelope."
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Byte offset to start reading from (use next_offset). Default 0.",
                        "default": 0
                    },
                    "length": {
                        "type": "integer",
                        "description": "Bytes to read this call (capped at 600000).",
                        "default": 600000
                    }
                },
                "required": []
            }
        ),
        types.Tool(
            name="get_ticket",
            description="Retrieve a Zendesk ticket by its ID",
            inputSchema={
                "type": "object",
                "properties": {
                    "ticket_id": {
                        "type": "integer",
                        "description": "The ID of the ticket to retrieve"
                    }
                },
                "required": ["ticket_id"]
            }
        ),
        types.Tool(
            name="create_ticket",
            description="Create a new Zendesk ticket",
            inputSchema={
                "type": "object",
                "properties": {
                    "subject": {"type": "string", "description": "Ticket subject"},
                    "description": {"type": "string", "description": "Ticket description"},
                    "requester_id": {"type": "integer"},
                    "assignee_id": {"type": "integer"},
                    "priority": {"type": "string", "description": "low, normal, high, urgent"},
                    "type": {"type": "string", "description": "problem, incident, question, task"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "custom_fields": {"type": "array", "items": {"type": "object"}},
                },
                "required": ["subject", "description"],
            }
        ),
        types.Tool(
            name="get_tickets",
            description="Fetch the latest tickets with pagination support",
            inputSchema={
                "type": "object",
                "properties": {
                    "page": {
                        "type": "integer",
                        "description": "Page number",
                        "default": 1
                    },
                    "per_page": {
                        "type": "integer",
                        "description": "Number of tickets per page (max 100)",
                        "default": 25
                    },
                    "sort_by": {
                        "type": "string",
                        "description": "Field to sort by (created_at, updated_at, priority, status)",
                        "default": "created_at"
                    },
                    "sort_order": {
                        "type": "string",
                        "description": "Sort order (asc or desc)",
                        "default": "desc"
                    }
                },
                "required": []
            }
        ),
        types.Tool(
            name="get_ticket_comments",
            description="Retrieve all comments for a Zendesk ticket by its ID",
            inputSchema={
                "type": "object",
                "properties": {
                    "ticket_id": {
                        "type": "integer",
                        "description": "The ID of the ticket to get comments for"
                    }
                },
                "required": ["ticket_id"]
            }
        ),
        types.Tool(
            name="get_ticket_comments_batch",
            description=(
                "Fetch all comments for multiple tickets in a single call. "
                "Takes an array of ticket ids and fetches them in parallel "
                "(thread pool), returning one entry per ticket in request order. "
                "Use this instead of calling get_ticket_comments once per ticket "
                "when reading across many tickets (e.g. to spot trends in what "
                "users wrote). A failing ticket returns {ticket_id, error} "
                "instead of sinking the batch."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "ticket_ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Zendesk ticket ids to fetch comments for"
                    }
                },
                "required": ["ticket_ids"]
            }
        ),
        types.Tool(
            name="search_ticket_comments",
            description=(
                "Keyword search over what users actually wrote — ticket subject, "
                "description, and comment bodies. Use this when looking for "
                "tickets that mention specific words or phrases (e.g. 'cannot "
                "export pdf', 'sync error'); pass plain words or a quoted phrase "
                "and they are full-text matched against ticket text. This is the "
                "`search` tool scoped to tickets; reach for `search` instead when "
                "you need field qualifiers like status:open or requester:..."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "Words or quoted phrase to find in ticket text"
                    },
                    "sort_by": {
                        "type": "string",
                        "description": "Field to sort by (created_at, updated_at, priority, status)"
                    },
                    "sort_order": {
                        "type": "string",
                        "description": "Sort order (asc or desc)"
                    },
                    "page": {
                        "type": "integer",
                        "description": "Page number",
                        "default": 1
                    },
                    "per_page": {
                        "type": "integer",
                        "description": "Results per page (max 100)",
                        "default": 25
                    }
                },
                "required": ["text"]
            }
        ),
        types.Tool(
            name="create_ticket_comment",
            description="Create a new comment on an existing Zendesk ticket",
            inputSchema={
                "type": "object",
                "properties": {
                    "ticket_id": {
                        "type": "integer",
                        "description": "The ID of the ticket to comment on"
                    },
                    "comment": {
                        "type": "string",
                        "description": "The comment text/content to add"
                    },
                    "public": {
                        "type": "boolean",
                        "description": "Whether the comment should be public",
                        "default": True
                    }
                },
                "required": ["ticket_id", "comment"]
            }
        ),
        types.Tool(
            name="get_ticket_attachment",
            description="Fetch a Zendesk ticket attachment by its content_url and return the file as base64-encoded data. Use the attachment URLs returned by get_ticket_comments. Supported types: safe images (jpeg/png/gif/webp) returned as ImageContent, and ZIP-shaped binary bundles (application/zip, application/x-zip-compressed, application/octet-stream, application/binary — covers Notability .ntb note bundles and logs.zip diagnostic bundles) returned as TextContent with JSON `{content_type, data_base64}`. ZIP magic-byte validation is enforced. 25 MB size cap.",
            inputSchema={
                "type": "object",
                "properties": {
                    "content_url": {
                        "type": "string",
                        "description": "The content_url of the attachment from get_ticket_comments"
                    }
                },
                "required": ["content_url"]
            }
        ),
        types.Tool(
            name="list_views",
            description="List all Zendesk views (filters/queues) with their ids and titles. Use this to resolve a view title like 'iOS Server Engineering Triage (Migrated Users)' to a numeric id without leaving the agent.",
            inputSchema={
                "type": "object",
                "properties": {
                    "active_only": {
                        "type": "boolean",
                        "description": "If true, return only active views. Default true.",
                        "default": True
                    }
                },
                "required": []
            }
        ),
        types.Tool(
            name="get_view_tickets",
            description="Return the tickets in a Zendesk view (filter/queue). The view_id is the numeric id from the view URL (e.g. https://notability.zendesk.com/agent/filters/10045803779738 -> 10045803779738). Returns a summary list (id, subject, status, tags, etc.) similar to get_tickets — call get_ticket on individual ids for full detail.",
            inputSchema={
                "type": "object",
                "properties": {
                    "view_id": {
                        "type": "integer",
                        "description": "Zendesk view id"
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max tickets to return (1-100)",
                        "default": 25
                    }
                },
                "required": ["view_id"]
            }
        ),
        types.Tool(
            name="get_views_batch",
            description="Fetch tickets for multiple Zendesk views in a single call. Takes an array of view ids and fetches them in parallel (thread pool), returning one result per view in request order. Far faster than calling get_view_tickets sequentially for several views. A failing view returns {view_id, error} instead of sinking the batch.",
            inputSchema={
                "type": "object",
                "properties": {
                    "view_ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Zendesk view ids to fetch tickets for"
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max tickets to return per view (1-100)",
                        "default": 25
                    }
                },
                "required": ["view_ids"]
            }
        ),
        types.Tool(
            name="list_ticket_fields",
            description="List all ticket field definitions in the Zendesk workspace, including custom fields. Use this to resolve custom_field ids (returned by get_ticket under custom_fields) to human-readable names like 'GitHub Issue #' or 'Fin Topic'.",
            inputSchema={
                "type": "object",
                "properties": {},
                "required": []
            }
        ),
        types.Tool(
            name="search",
            description=(
                "Full-text search across Zendesk via the Search API. Unlike views, "
                "this searches ticket bodies, comments, subjects, tags, and more. "
                "Use this to find tickets by words in their body/description. "
                "Supports Zendesk query syntax (e.g. 'crash status:open', "
                "'type:ticket \"login error\"', 'requester:user@example.com')."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Zendesk search query string. Supports full-text terms and field qualifiers (status, priority, requester, created>2024-01-01, etc.)."
                    },
                    "type": {
                        "type": "string",
                        "description": "Restrict results to a type: ticket, user, organization, or group"
                    },
                    "sort_by": {
                        "type": "string",
                        "description": "Field to sort by (created_at, updated_at, priority, status, ticket_type)"
                    },
                    "sort_order": {
                        "type": "string",
                        "description": "Sort order (asc or desc)"
                    },
                    "page": {
                        "type": "integer",
                        "description": "Page number",
                        "default": 1
                    },
                    "per_page": {
                        "type": "integer",
                        "description": "Results per page (max 100)",
                        "default": 25
                    }
                },
                "required": ["query"]
            }
        ),
        types.Tool(
            name="get_satisfaction_ratings",
            description=(
                "List CSAT / satisfaction ratings via the Zendesk Satisfaction "
                "Ratings API. Each rating includes assignee_id, score (good/bad), "
                "comment, ticket_id, and timestamps. There is no server-side "
                "agent filter, so to rank CSAT per agent: fetch ratings (optionally "
                "filtered by score and date range), then group by assignee_id "
                "client-side. Use score='received' to limit to ratings customers "
                "actually submitted, or 'good'/'bad' for a single sentiment. Filter "
                "by date with start_time/end_time as Unix epoch seconds."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "score": {
                        "type": "string",
                        "description": "Score filter: offered, unoffered, received, received_with_comment, received_without_comment, good, good_with_comment, good_without_comment, bad, bad_with_comment, bad_without_comment"
                    },
                    "start_time": {
                        "type": "integer",
                        "description": "Only ratings created at/after this Unix epoch (seconds)"
                    },
                    "end_time": {
                        "type": "integer",
                        "description": "Only ratings created at/before this Unix epoch (seconds)"
                    },
                    "page": {
                        "type": "integer",
                        "description": "Page number",
                        "default": 1
                    },
                    "per_page": {
                        "type": "integer",
                        "description": "Results per page (max 100)",
                        "default": 100
                    }
                },
                "required": []
            }
        ),
        types.Tool(
            name="get_ticket_metrics",
            description=(
                "Fetch the metric set for a single ticket (Zendesk Ticket "
                "Metrics API). Returns durations you can't get from views or "
                "the ticket object: first reply time, first/full resolution "
                "time, agent/requester wait time, and time spent in each "
                "status — each reported in both calendar and business minutes. "
                "Use this to compute KPIs like average first response time or "
                "resolution time per agent."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "ticket_id": {
                        "type": "integer",
                        "description": "The ID of the ticket to fetch metrics for"
                    }
                },
                "required": ["ticket_id"]
            }
        ),
        types.Tool(
            name="get_users",
            description=(
                "Resolve a batch of Zendesk user ids to their profiles (name, "
                "email, role, organization) in a single call. Tickets reference "
                "people only by numeric id (requester_id, assignee_id), so use "
                "this to turn those ids into human-readable names/emails — e.g. "
                "to show who is waiting on a queue of tickets. Up to 100 ids "
                "per call."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "user_ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Zendesk user ids to resolve (max 100)"
                    }
                },
                "required": ["user_ids"]
            }
        ),
        types.Tool(
            name="get_ticket_counts_by_status",
            description=(
                "Return per-status ticket counts in a single call, optionally "
                "scoped to one agent, using the Zendesk Search Count API. "
                "Instead of one view (or full ticket fetch) per status, this "
                "fans out cheap count-only queries in parallel and returns a "
                "{status: count} map plus a total. Defaults to the active "
                "workload (new, open, pending, hold). Use 'hold' for the "
                "on-hold status. Ideal for a lightweight per-agent workload "
                "summary on a dashboard."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "assignee_id": {
                        "type": "integer",
                        "description": "Optional agent id to scope counts to. Omit for workspace-wide counts."
                    },
                    "statuses": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": ["new", "open", "pending", "hold", "solved", "closed"]
                        },
                        "description": "Statuses to count (new, open, pending, hold, solved, closed). Defaults to [new, open, pending, hold]."
                    }
                },
                "required": []
            }
        ),
        types.Tool(
            name="update_ticket",
            description="Update fields on an existing Zendesk ticket (e.g., status, priority, assignee_id)",
            inputSchema={
                "type": "object",
                "properties": {
                    "ticket_id": {"type": "integer", "description": "The ID of the ticket to update"},
                    "subject": {"type": "string"},
                    "status": {"type": "string", "description": "new, open, pending, on-hold, solved, closed"},
                    "priority": {"type": "string", "description": "low, normal, high, urgent"},
                    "type": {"type": "string"},
                    "assignee_id": {"type": "integer"},
                    "requester_id": {"type": "integer"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "custom_fields": {"type": "array", "items": {"type": "object"}},
                    "due_at": {"type": "string", "description": "ISO8601 datetime"}
                },
                "required": ["ticket_id"]
            }
        )
    ]


# --- Oversized-result guard -------------------------------------------------
#
# MCP hosts reject any single tool result over 1 MB ("Tool result is too large.
# Maximum size is 1MB"). Several tools here can blow past that (attachment
# base64, long comment threads, wide search pages). Rather than fix each tool,
# every result passes through _guard_result: if it fits, it goes through
# untouched; if not, the full payload is spilled to a temp file and the caller
# gets a small envelope (preview + token + path + next_offset). The full data is
# then retrievable two ways: read the file directly (saved_to), or page through
# it byte-for-byte with the fetch_result_chunk tool (host-agnostic, no
# filesystem access required).

# Stay under the 1 MB hard cap with headroom for the JSON-RPC envelope.
MAX_RESULT_BYTES = 900_000
# How much of an oversized text payload to inline as a human/LLM-readable preview.
PREVIEW_BYTES = 16_000
# Default chunk size for fetch_result_chunk. Base64 inflates ~33%, so a 600 KB
# raw slice serializes to ~800 KB — comfortably under the cap.
CHUNK_BYTES = 600_000
# Spilled files are bounded by age, not count: anything older than this is swept
# on the next spill. Generous enough that a caller paging a fresh result with
# fetch_result_chunk will never see it disappear mid-read.
SPILL_TTL_SECONDS = 24 * 60 * 60


def _spill_dir() -> Path:
    """Directory holding spilled oversized results. Override with ZENDESK_MCP_SPILL_DIR."""
    override = os.getenv("ZENDESK_MCP_SPILL_DIR")
    base = Path(override) if override else Path(tempfile.gettempdir()) / "zendesk-mcp-results"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _sweep_old_spills(base: Path) -> None:
    """Best-effort delete of spill files older than SPILL_TTL_SECONDS. Never raises."""
    cutoff = time.time() - SPILL_TTL_SECONDS
    try:
        entries = list(base.iterdir())
    except OSError:
        return
    for entry in entries:
        try:
            if entry.is_file() and entry.stat().st_mtime < cutoff:
                entry.unlink()
        except OSError:
            continue


def _content_bytes(item: Any) -> int:
    if isinstance(item, types.TextContent):
        return len(item.text.encode("utf-8"))
    if isinstance(item, types.ImageContent):
        return len(item.data.encode("ascii"))
    return 0


def _spill_envelope(token: str, path: Path, total: int, *, preview: str | None,
                    mime_type: str | None = None) -> types.TextContent:
    envelope: Dict[str, Any] = {
        "truncated": True,
        "reason": f"Full result is {total} bytes, over the {MAX_RESULT_BYTES}-byte MCP cap.",
        "total_bytes": total,
        "result_token": token,
        "saved_to": str(path),
        # The chunk stream always starts at byte 0 — preview is a display
        # convenience, NOT part of the reconstructable byte stream.
        "next_offset": 0,
        "how_to_get_full": (
            "Read the file at saved_to directly, OR call fetch_result_chunk with "
            "this result_token (or saved_to as 'path') starting at next_offset (0), "
            "repeating until next_offset is null. Chunk data is base64 of the raw "
            "byte slice — concatenate the decoded bytes to reconstruct the payload. "
            "The preview is a lossy head excerpt for display only; do not use it to "
            "rebuild the payload."
        ),
    }
    if preview is not None:
        envelope["preview"] = preview
        envelope["preview_bytes"] = len(preview.encode("utf-8"))
    if mime_type is not None:
        envelope["mime_type"] = mime_type
    return types.TextContent(type="text", text=json.dumps(envelope, indent=2))


def _guard_result(contents: list[Any]) -> list[Any]:
    """Pass results through untouched if they fit; otherwise spill + return an envelope."""
    total = sum(_content_bytes(item) for item in contents)
    if total <= MAX_RESULT_BYTES:
        return contents

    # Every tool handler returns exactly one content item (one TextContent or
    # one ImageContent); spilling only ever deals with that single payload.
    item = contents[0]
    token = secrets.token_hex(8)
    _sweep_old_spills(_spill_dir())

    # Image / binary payload: spill the decoded bytes, no text preview.
    if isinstance(item, types.ImageContent):
        raw = base64.b64decode(item.data)
        ext = (item.mimeType.split("/")[-1] if item.mimeType else "bin") or "bin"
        path = _spill_dir() / f"{token}.{ext}"
        path.write_bytes(raw)
        return [_spill_envelope(token, path, len(raw), preview=None, mime_type=item.mimeType)]

    # Text payload (every JSON tool): spill UTF-8, inline a head preview.
    data = item.text.encode("utf-8")
    path = _spill_dir() / f"{token}.json"
    path.write_bytes(data)
    # Slice the decoded string, not the bytes, so the preview can't end on a
    # split multibyte sequence.
    preview = item.text[:PREVIEW_BYTES]
    return [_spill_envelope(token, path, len(data), preview=preview)]


def _resolve_spill_path(arguments: dict[str, Any]) -> Path:
    """Resolve a fetch_result_chunk target to a path inside the spill dir (no traversal)."""
    spill = _spill_dir().resolve()
    raw_path = arguments.get("path")
    token = arguments.get("result_token")
    if raw_path:
        candidate = Path(raw_path).resolve()
    elif token:
        matches = list(spill.glob(f"{token}.*"))
        if not matches:
            raise ValueError(f"No spilled result found for token '{token}' (it may have been cleaned up).")
        candidate = matches[0].resolve()
    else:
        raise ValueError("fetch_result_chunk requires 'result_token' or 'path'.")

    if spill not in candidate.parents:
        raise ValueError("Refusing to read a path outside the spill directory.")
    if not candidate.is_file():
        raise ValueError(f"Spilled result file not found: {candidate}")
    return candidate


def _fetch_result_chunk(arguments: dict[str, Any]) -> Dict[str, Any]:
    path = _resolve_spill_path(arguments)
    offset = int(arguments.get("offset", 0))
    length = int(arguments.get("length", CHUNK_BYTES))
    length = max(1, min(length, CHUNK_BYTES))
    total = path.stat().st_size
    if offset < 0 or offset > total:
        raise ValueError(f"offset {offset} out of range for {total}-byte result.")

    with path.open("rb") as fh:
        fh.seek(offset)
        chunk = fh.read(length)
    next_offset = offset + len(chunk)
    eof = next_offset >= total
    return {
        "result_token": arguments.get("result_token"),
        "path": str(path),
        "offset": offset,
        "length": len(chunk),
        "total_bytes": total,
        "next_offset": None if eof else next_offset,
        "eof": eof,
        "encoding": "base64",
        "data_base64": base64.b64encode(chunk).decode("ascii"),
    }


@server.call_tool()
async def handle_call_tool(
        name: str,
        arguments: dict[str, Any] | None
) -> list[types.TextContent]:
    """Handle Zendesk tool execution requests"""
    try:
        return _guard_result(_dispatch_tool(name, arguments))
    except Exception as e:
        return [types.TextContent(
            type="text",
            text=f"Error: {str(e)}"
        )]


def _dispatch_tool(
        name: str,
        arguments: dict[str, Any] | None
) -> list[Any]:
    """Route a tool call to its handler and return raw content (pre-guard)."""
    if name == "fetch_result_chunk":
        return [types.TextContent(
            type="text",
            text=json.dumps(_fetch_result_chunk(arguments or {}), indent=2)
        )]

    elif name == "get_ticket":
        if not arguments:
            raise ValueError("Missing arguments")
        ticket = zendesk_client.get_ticket(arguments["ticket_id"])
        return [types.TextContent(
            type="text",
            text=json.dumps(ticket)
        )]

    elif name == "list_views":
        active_only = True
        if arguments and "active_only" in arguments:
            active_only = bool(arguments["active_only"])
        views = zendesk_client.list_views(active_only=active_only)
        return [types.TextContent(
            type="text",
            text=json.dumps(views)
        )]

    elif name == "get_view_tickets":
        if not arguments:
            raise ValueError("Missing arguments")
        result = zendesk_client.get_view_tickets(
            view_id=arguments["view_id"],
            limit=arguments.get("limit", 25),
        )
        return [types.TextContent(
            type="text",
            text=json.dumps(result)
        )]

    elif name == "get_views_batch":
        if not arguments or "view_ids" not in arguments:
            raise ValueError("Missing required argument: view_ids")
        result = zendesk_client.get_views_batch(
            view_ids=arguments["view_ids"],
            limit=arguments.get("limit", 25),
        )
        return [types.TextContent(
            type="text",
            text=json.dumps(result)
        )]

    elif name == "list_ticket_fields":
        fields = zendesk_client.list_ticket_fields()
        return [types.TextContent(
            type="text",
            text=json.dumps(fields)
        )]

    elif name == "create_ticket":
        if not arguments:
            raise ValueError("Missing arguments")
        created = zendesk_client.create_ticket(
            subject=arguments.get("subject"),
            description=arguments.get("description"),
            requester_id=arguments.get("requester_id"),
            assignee_id=arguments.get("assignee_id"),
            priority=arguments.get("priority"),
            type=arguments.get("type"),
            tags=arguments.get("tags"),
            custom_fields=arguments.get("custom_fields"),
        )
        return [types.TextContent(
            type="text",
            text=json.dumps({"message": "Ticket created successfully", "ticket": created}, indent=2)
        )]

    elif name == "get_tickets":
        page = arguments.get("page", 1) if arguments else 1
        per_page = arguments.get("per_page", 25) if arguments else 25
        sort_by = arguments.get("sort_by", "created_at") if arguments else "created_at"
        sort_order = arguments.get("sort_order", "desc") if arguments else "desc"

        tickets = zendesk_client.get_tickets(
            page=page,
            per_page=per_page,
            sort_by=sort_by,
            sort_order=sort_order
        )
        return [types.TextContent(
            type="text",
            text=json.dumps(tickets, indent=2)
        )]

    elif name == "get_ticket_comments":
        if not arguments:
            raise ValueError("Missing arguments")
        comments = zendesk_client.get_ticket_comments(
            arguments["ticket_id"])
        return [types.TextContent(
            type="text",
            text=json.dumps(comments)
        )]

    elif name == "get_ticket_comments_batch":
        if not arguments or "ticket_ids" not in arguments:
            raise ValueError("Missing required argument: ticket_ids")
        result = zendesk_client.get_ticket_comments_batch(arguments["ticket_ids"])
        return [types.TextContent(
            type="text",
            text=json.dumps(result)
        )]

    elif name == "search_ticket_comments":
        if not arguments or not arguments.get("text"):
            raise ValueError("Missing required argument: text")
        results = zendesk_client.search_ticket_comments(
            text=arguments["text"],
            sort_by=arguments.get("sort_by"),
            sort_order=arguments.get("sort_order"),
            page=arguments.get("page", 1),
            per_page=arguments.get("per_page", 25),
        )
        return [types.TextContent(
            type="text",
            text=json.dumps(results, indent=2)
        )]

    elif name == "create_ticket_comment":
        if not arguments:
            raise ValueError("Missing arguments")
        public = arguments.get("public", True)
        result = zendesk_client.post_comment(
            ticket_id=arguments["ticket_id"],
            comment=arguments["comment"],
            public=public
        )
        return [types.TextContent(
            type="text",
            text=f"Comment created successfully: {result}"
        )]

    elif name == "get_ticket_attachment":
        if not arguments:
            raise ValueError("Missing arguments")
        result = zendesk_client.get_ticket_attachment(arguments["content_url"])
        content_type = result["content_type"]
        if content_type.startswith("image/"):
            return [types.ImageContent(
                type="image",
                data=result["data"],
                mimeType=content_type,
            )]
        else:
            return [types.TextContent(
                type="text",
                text=json.dumps({"content_type": content_type, "data_base64": result["data"]})
            )]

    elif name == "search":
        if not arguments or not arguments.get("query"):
            raise ValueError("Missing required argument: query")
        results = zendesk_client.search(
            query=arguments["query"],
            type=arguments.get("type"),
            sort_by=arguments.get("sort_by"),
            sort_order=arguments.get("sort_order"),
            page=arguments.get("page", 1),
            per_page=arguments.get("per_page", 25),
        )
        return [types.TextContent(
            type="text",
            text=json.dumps(results, indent=2)
        )]

    elif name == "get_satisfaction_ratings":
        args = arguments or {}
        results = zendesk_client.get_satisfaction_ratings(
            score=args.get("score"),
            start_time=args.get("start_time"),
            end_time=args.get("end_time"),
            page=args.get("page", 1),
            per_page=args.get("per_page", 100),
        )
        return [types.TextContent(
            type="text",
            text=json.dumps(results, indent=2)
        )]

    elif name == "get_ticket_metrics":
        if not arguments or "ticket_id" not in arguments:
            raise ValueError("Missing required argument: ticket_id")
        metrics = zendesk_client.get_ticket_metrics(arguments["ticket_id"])
        return [types.TextContent(
            type="text",
            text=json.dumps(metrics, indent=2)
        )]

    elif name == "get_users":
        if not arguments or "user_ids" not in arguments:
            raise ValueError("Missing required argument: user_ids")
        result = zendesk_client.get_users(arguments["user_ids"])
        return [types.TextContent(
            type="text",
            text=json.dumps(result, indent=2)
        )]

    elif name == "get_ticket_counts_by_status":
        args = arguments or {}
        result = zendesk_client.get_ticket_counts_by_status(
            assignee_id=args.get("assignee_id"),
            statuses=args.get("statuses"),
        )
        return [types.TextContent(
            type="text",
            text=json.dumps(result, indent=2)
        )]

    elif name == "update_ticket":
        if not arguments:
            raise ValueError("Missing arguments")
        ticket_id = arguments.get("ticket_id")
        if ticket_id is None:
            raise ValueError("ticket_id is required")
        update_fields = {k: v for k, v in arguments.items() if k != "ticket_id"}
        updated = zendesk_client.update_ticket(ticket_id=int(ticket_id), **update_fields)
        return [types.TextContent(
            type="text",
            text=json.dumps({"message": "Ticket updated successfully", "ticket": updated}, indent=2)
        )]

    else:
        raise ValueError(f"Unknown tool: {name}")


@server.list_resources()
async def handle_list_resources() -> list[types.Resource]:
    logger.debug("Handling list_resources request")
    return [
        types.Resource(
            uri=AnyUrl("zendesk://knowledge-base"),
            name="Zendesk Knowledge Base",
            description="Access to Zendesk Help Center articles and sections",
            mimeType="application/json",
        )
    ]


@ttl_cache(ttl=3600)
def get_cached_kb():
    return zendesk_client.get_all_articles()


@server.read_resource()
async def handle_read_resource(uri: AnyUrl) -> str:
    logger.debug(f"Handling read_resource request for URI: {uri}")
    if uri.scheme != "zendesk":
        logger.error(f"Unsupported URI scheme: {uri.scheme}")
        raise ValueError(f"Unsupported URI scheme: {uri.scheme}")

    path = str(uri).replace("zendesk://", "")
    if path != "knowledge-base":
        logger.error(f"Unknown resource path: {path}")
        raise ValueError(f"Unknown resource path: {path}")

    try:
        kb_data = get_cached_kb()
        return json.dumps({
            "knowledge_base": kb_data,
            "metadata": {
                "sections": len(kb_data),
                "total_articles": sum(len(section['articles']) for section in kb_data.values()),
            }
        }, indent=2)
    except Exception as e:
        logger.error(f"Error fetching knowledge base: {e}")
        raise


def build_app():
    """Build the ASGI app that serves this MCP server over streamable HTTP.

    The same `server` instance (handlers, tool registration, ZendeskClient) is
    wrapped in the SDK's streamable-HTTP session manager and mounted at /mcp.
    No tool logic changes — only the transport.
    """
    import contextlib

    from starlette.applications import Starlette
    from starlette.routing import Mount
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

    session_manager = StreamableHTTPSessionManager(app=server, json_response=False)

    async def handle_mcp(scope, receive, send):
        await session_manager.handle_request(scope, receive, send)

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        async with session_manager.run():
            yield

    return Starlette(routes=[Mount("/mcp", app=handle_mcp)], lifespan=lifespan)


def main_http():
    """Serve the MCP server over HTTP on 0.0.0.0:$PORT (default 8080)."""
    import uvicorn

    port = int(os.getenv("PORT", "8080"))
    logger.info(f"serving Zendesk MCP over HTTP on 0.0.0.0:{port}/mcp")
    uvicorn.run(build_app(), host="0.0.0.0", port=port)


async def main():
    # Run the server using stdin/stdout streams. Both stdio and HTTP advertise
    # the same identity via create_initialization_options() (see `server` above).
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream=read_stream,
            write_stream=write_stream,
            initialization_options=server.create_initialization_options(),
        )


if __name__ == "__main__":
    asyncio.run(main())
