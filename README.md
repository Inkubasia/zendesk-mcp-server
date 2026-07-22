# Zendesk MCP Server

![ci](https://github.com/Ginger-Labs/zendesk-mcp-server/actions/workflows/ci.yml/badge.svg)
[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)

A Model Context Protocol server for Zendesk.

This server provides a comprehensive integration with Zendesk. It offers:

- Tools for retrieving and managing Zendesk tickets and comments
- Specialized prompts for ticket analysis and response drafting
- Full access to the Zendesk Help Center articles as knowledge base

![demo](https://res.cloudinary.com/leecy-me/image/upload/v1736410626/open/zendesk_yunczu.gif)

## Setup

- build: `uv venv && uv pip install -e .` or `uv build` in short.
- setup zendesk credentials in `.env` file, refer to [.env.example](.env.example).
- configure in Claude desktop:

```json
{
  "mcpServers": {
      "zendesk": {
          "command": "uv",
          "args": [
              "--directory",
              "/path/to/zendesk-mcp-server",
              "run",
              "zendesk"
          ]
      }
  }
}
```

### Docker

You can containerize the server if you prefer an isolated runtime:

1. Copy `.env.example` to `.env` and fill in your Zendesk credentials. Keep this file outside version control.
2. Build the image:

   ```bash
   docker build -t zendesk-mcp-server .
   ```

3. Run the server, providing the environment file.

   By default the image serves the **stdio** transport (`CMD zendesk`) — the mode
   Claude Desktop/Code speak over STDIN/STDOUT; add `-i` when wiring it to a client:

   ```bash
   docker run --rm -i --env-file /path/to/.env zendesk-mcp-server
   ```

   To serve MCP over **streamable HTTP** instead, override the command with the
   `zendesk-http` entrypoint (serves on `$PORT`, default `8080`, at `/mcp`) and
   publish the port:

   ```bash
   docker run --rm -p 8080:8080 --env-file /path/to/.env zendesk-mcp-server zendesk-http
   # -> http://localhost:8080/mcp
   ```

   For daemonized runs, add `-d --name zendesk-mcp`.

The image installs dependencies from `requirements.lock`, drops privileges to a non-root user, and expects configuration exclusively via environment variables.

#### Claude MCP Integration

To use the Dockerized server from Claude Code/Desktop over stdio, add an entry to Claude Code's `settings.json` similar to:

```json
{
  "mcpServers": {
    "zendesk": {
      "command": "/usr/local/bin/docker",
      "args": [
        "run",
        "--rm",
        "-i",
        "--env-file",
        "/path/to/zendesk-mcp-server/.env",
        "zendesk-mcp-server"
      ]
    }
  }
}
```

Adjust the paths to match your environment. After saving the file, restart Claude for the new MCP server to be detected.

## Resources

- zendesk://knowledge-base, get access to the whole help center articles.

## Prompts

### analyze-ticket

Analyze a Zendesk ticket and provide a detailed analysis of the ticket.

### draft-ticket-response

Draft a response to a Zendesk ticket.

## Tools

### get_tickets

Fetch the latest tickets with pagination support

- Input:
  - `page` (integer, optional): Page number (defaults to 1)
  - `per_page` (integer, optional): Number of tickets per page, max 100 (defaults to 25)
  - `sort_by` (string, optional): Field to sort by - created_at, updated_at, priority, or status (defaults to created_at)
  - `sort_order` (string, optional): Sort order - asc or desc (defaults to desc)

- Output: Returns a list of tickets with essential fields including id, subject, status, priority, description, timestamps, and assignee information, along with pagination metadata

### search

Full-text search across Zendesk via the Search API (`/api/v2/search.json`). Unlike views, this searches ticket **bodies**, comments, subjects, tags, and more — use it to find tickets by words in their description/body.

- Input:
  - `query` (string): Zendesk search query string. Supports full-text terms and field qualifiers, e.g. `crash status:open`, `type:ticket "login error"`, `requester:user@example.com`, `created>2024-01-01`. See [Zendesk search reference](https://support.zendesk.com/hc/en-us/articles/4408886879258).
  - `type` (string, optional): Restrict results to one of `ticket`, `user`, `organization`, `group`
  - `sort_by` (string, optional): Field to sort by - created_at, updated_at, priority, status, or ticket_type
  - `sort_order` (string, optional): Sort order - asc or desc
  - `page` (integer, optional): Page number (defaults to 1)
  - `per_page` (integer, optional): Results per page, max 100 (defaults to 25)

- Output: Returns matching results with pagination metadata (count, has_more, next_page, previous_page)

### search_ticket_comments

Keyword search over what users actually wrote — ticket subject, description, and comment bodies. This is the `search` tool scoped to `type:ticket`: pass plain words or a quoted phrase (e.g. `cannot export pdf`, `"sync error"`) and they are full-text matched against ticket text. Reach for `search` instead when you need field qualifiers like `status:open` or `requester:...`.

- Input:
  - `text` (string): Words or quoted phrase to find in ticket text
  - `sort_by` (string, optional): Field to sort by - created_at, updated_at, priority, status
  - `sort_order` (string, optional): Sort order - asc or desc
  - `page` (integer, optional): Page number (defaults to 1)
  - `per_page` (integer, optional): Results per page, max 100 (defaults to 25)

- Output: Same shape as `search` — trimmed ticket summaries with pagination metadata

### get_satisfaction_ratings

List CSAT / satisfaction ratings via the Satisfaction Ratings API (`/api/v2/satisfaction_ratings.json`). Each rating includes `assignee_id`, `score` (good/bad), `comment`, `ticket_id`, and timestamps. There is no server-side agent filter — to rank CSAT per agent, fetch ratings (optionally filtered by score and date range) and group by `assignee_id` client-side.

- Input:
  - `score` (string, optional): Score filter - `offered`, `unoffered`, `received`, `received_with_comment`, `received_without_comment`, `good`, `good_with_comment`, `good_without_comment`, `bad`, `bad_with_comment`, `bad_without_comment`
  - `start_time` (integer, optional): Only ratings created at/after this Unix epoch (seconds)
  - `end_time` (integer, optional): Only ratings created at/before this Unix epoch (seconds)
  - `page` (integer, optional): Page number (defaults to 1)
  - `per_page` (integer, optional): Results per page, max 100 (defaults to 100)

- Output: Returns `satisfaction_ratings` (raw rating objects) with pagination metadata (count, has_more, next_page, previous_page)

### get_ticket_metrics

Fetch the metric set for a single ticket via the Ticket Metrics API (`/api/v2/tickets/{id}/metrics.json`). Views and the ticket object expose status and timestamps but **not the durations** support teams report on — this returns them. Each duration is reported in both calendar and business (schedule) minutes, so you can compute KPIs like average first response time or resolution time per agent without deriving them yourself.

- Input:
  - `ticket_id` (integer): The ID of the ticket to fetch metrics for

- Output: The raw `ticket_metric` object, including `reply_time_in_minutes`, `first_resolution_time_in_minutes`, `full_resolution_time_in_minutes`, agent/requester wait times, the `*_breaches` counters, and the per-status `*_at` timestamps

### get_users

Resolve a batch of user ids to their profiles in a single request via the Show Many Users API (`/api/v2/users/show_many.json`). Tickets reference people only by numeric id (`requester_id`, `assignee_id`), so use this to turn those ids into human-readable names and emails — for example, to display "who is waiting" across a queue of tickets — without one request per user.

- Input:
  - `user_ids` (array[integer]): Zendesk user ids to resolve (max 100 per call)

- Output: `count` and `users`, a list of trimmed profiles (`id`, `name`, `email`, `role`, `active`, `organization_id`, `time_zone`, timestamps)

### get_ticket_counts_by_status

Return per-status ticket counts in a single call, optionally scoped to one agent, via the Search Count API (`/api/v2/search/count.json`). Instead of one view (or a full ticket fetch) per status, this fans out cheap count-only queries in parallel and returns a `{status: count}` map plus a total — ideal for a lightweight per-agent workload summary on a dashboard.

- Input:
  - `assignee_id` (integer, optional): Agent id to scope counts to. Omit for workspace-wide counts.
  - `statuses` (array[string], optional): Statuses to count — `new`, `open`, `pending`, `hold`, `solved`, `closed`. Defaults to the active workload `[new, open, pending, hold]`. Use `hold` for the on-hold status.

- Output: `assignee_id`, `counts` (status → `{count}`, or `{error}` if that status query failed — one bad query never sinks the rest), and `total` (sum across statuses that succeeded)

### list_views

List all Zendesk views (filters/queues) with their ids and titles. Use this to resolve a view title to a numeric id.

- Input:
  - `active_only` (boolean, optional): If true, return only active views (defaults to true)

### get_view_tickets

Return the tickets in a Zendesk view (filter/queue). The `view_id` is the numeric id from the view URL (e.g. `.../agent/filters/10045803779738` → `10045803779738`). Returns a summary list — call `get_ticket` for full detail.

- Input:
  - `view_id` (integer): Zendesk view id
  - `limit` (integer, optional): Max tickets to return, 1-100 (defaults to 25)

### list_ticket_fields

List all ticket field definitions in the workspace, including custom fields. Use this to resolve custom field ids (from `get_ticket`'s `custom_fields`) to human-readable names.

- Input: none

### get_ticket

Retrieve a Zendesk ticket by its ID

- Input:
  - `ticket_id` (integer): The ID of the ticket to retrieve

### get_ticket_comments

Retrieve all comments for a Zendesk ticket by its ID

- Input:
  - `ticket_id` (integer): The ID of the ticket to get comments for

### get_ticket_comments_batch

Fetch all comments for multiple tickets in a single call. Takes an array of ticket ids and fetches them in parallel (thread pool), returning one entry per ticket in request order. Use this instead of calling `get_ticket_comments` once per ticket when reading across many tickets — for example, to spot trends in what users wrote.

- Input:
  - `ticket_ids` (array[integer]): Zendesk ticket ids to fetch comments for

- Output: `count` and `tickets`, a list of per-ticket results in request order — each either `{ticket_id, comments}` (same comment shape as `get_ticket_comments`) or `{ticket_id, error}` if that ticket failed (one bad ticket never sinks the batch)

### get_ticket_attachment

Fetch a ticket attachment by its `content_url` (from `get_ticket_comments`) and return it as base64-encoded data. Restricted to safe image types (jpeg, png, gif, webp) with magic-byte validation and a 10 MB size cap.

- Input:
  - `content_url` (string): The `content_url` of the attachment, as returned by `get_ticket_comments`

### create_ticket_comment

Create a new comment on an existing Zendesk ticket

- Input:
  - `ticket_id` (integer): The ID of the ticket to comment on
  - `comment` (string): The comment text/content to add
  - `public` (boolean, optional): Whether the comment should be public (defaults to true)

### create_ticket

Create a new Zendesk ticket

- Input:
  - `subject` (string): Ticket subject
  - `description` (string): Ticket description
  - `requester_id` (integer, optional)
  - `assignee_id` (integer, optional)
  - `priority` (string, optional): one of `low`, `normal`, `high`, `urgent`
  - `type` (string, optional): one of `problem`, `incident`, `question`, `task`
  - `tags` (array[string], optional)
  - `custom_fields` (array[object], optional)

### update_ticket

Update fields on an existing Zendesk ticket (e.g., status, priority, assignee)

- Input:
  - `ticket_id` (integer): The ID of the ticket to update
  - `subject` (string, optional)
  - `status` (string, optional): one of `new`, `open`, `pending`, `on-hold`, `solved`, `closed`
  - `priority` (string, optional): one of `low`, `normal`, `high`, `urgent`
  - `type` (string, optional)
  - `assignee_id` (integer, optional)
  - `requester_id` (integer, optional)
  - `tags` (array[string], optional)
  - `custom_fields` (array[object], optional)
  - `due_at` (string, optional): ISO8601 datetime
