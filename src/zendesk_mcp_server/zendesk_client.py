from typing import Dict, Any, List
from concurrent.futures import ThreadPoolExecutor
import json
import urllib.request
import urllib.parse
import base64
import requests as _requests
from urllib3.util import parse_url as _parse_url

from zenpy import Zenpy
from zenpy.lib.api_objects import Comment
from zenpy.lib.api_objects import Ticket as ZenpyTicket


class ZendeskClient:
    def __init__(self, subdomain: str, email: str, token: str):
        """
        Initialize the Zendesk client using zenpy lib and direct API.
        """
        self.client = Zenpy(
            subdomain=subdomain,
            email=email,
            token=token
        )

        # For direct API calls
        self.subdomain = subdomain
        self.email = email
        self.token = token
        self.base_url = f"https://{subdomain}.zendesk.com/api/v2"
        # Create basic auth header
        credentials = f"{email}/token:{token}"
        encoded_credentials = base64.b64encode(credentials.encode()).decode('ascii')
        self.auth_header = f"Basic {encoded_credentials}"

    def get_ticket(self, ticket_id: int) -> Dict[str, Any]:
        """
        Query a ticket by its ID
        """
        try:
            ticket = self.client.tickets(id=ticket_id)
            raw_custom_fields = getattr(ticket, 'custom_fields', None) or []
            custom_fields = [
                {'id': cf.get('id'), 'value': cf.get('value')} if isinstance(cf, dict)
                else {'id': getattr(cf, 'id', None), 'value': getattr(cf, 'value', None)}
                for cf in raw_custom_fields
            ]
            tags = list(getattr(ticket, 'tags', None) or [])
            return {
                'id': ticket.id,
                'subject': ticket.subject,
                'description': ticket.description,
                'status': ticket.status,
                'priority': ticket.priority,
                'created_at': str(ticket.created_at),
                'updated_at': str(ticket.updated_at),
                'requester_id': ticket.requester_id,
                'assignee_id': ticket.assignee_id,
                'organization_id': ticket.organization_id,
                'tags': tags,
                'custom_fields': custom_fields,
            }
        except Exception as e:
            raise Exception(f"Failed to get ticket {ticket_id}: {str(e)}")

    def get_view_tickets(self, view_id: int, limit: int = 25) -> Dict[str, Any]:
        """
        Return tickets in a Zendesk view (filter/queue), plus the view's own
        metadata (title, description, etc.).

        Args:
            view_id: The Zendesk view id (the numeric id in the view URL).
            limit: Max tickets to return. Capped at 100.

        Returns:
            Dict with `view` (view metadata), `count`, `has_more` (whether the
            view contains tickets beyond `limit`), and `tickets` (summary list
            — id, subject, status, priority, timestamps, requester_id,
            assignee_id, tags). `description` is intentionally omitted from the
            per-ticket summary because descriptions can be multi-KB and blow
            the token budget at limit=100; call `get_ticket` for full detail.
        """
        try:
            limit = max(1, min(limit, 100))
            view = self.client.views(id=view_id)
            view_meta = {
                'id': view.id,
                'title': getattr(view, 'title', None),
                'description': getattr(view, 'description', None),
                'active': getattr(view, 'active', None),
                'position': getattr(view, 'position', None),
            }

            ticket_gen = self.client.views.tickets(view=view_id)
            tickets: List[Dict[str, Any]] = []
            has_more = False
            for t in ticket_gen:
                if len(tickets) >= limit:
                    has_more = True
                    break
                tickets.append({
                    'id': t.id,
                    'subject': t.subject,
                    'status': t.status,
                    'priority': t.priority,
                    'created_at': str(t.created_at),
                    'updated_at': str(t.updated_at),
                    'requester_id': t.requester_id,
                    'assignee_id': t.assignee_id,
                    'tags': list(getattr(t, 'tags', None) or []),
                })
            return {
                'view': view_meta,
                'count': len(tickets),
                'has_more': has_more,
                'tickets': tickets,
            }
        except Exception as e:
            raise Exception(f"Failed to get tickets for view {view_id}: {str(e)}")

    def get_views_batch(self, view_ids: List[int], limit: int = 25) -> Dict[str, Any]:
        """
        Fetch tickets for several views in parallel.

        zenpy's API client is blocking/synchronous, so each view fetch would
        otherwise run serially. We fan the per-view calls out across a thread
        pool (the Python equivalent of JS `Promise.all`) so the wall-clock cost
        is roughly that of the slowest single view rather than their sum.

        Args:
            view_ids: List of Zendesk view ids to fetch.
            limit: Max tickets to return per view. Capped at 100.

        Returns:
            Dict with `count` (number of views requested) and `views`, a list
            of per-view results in the same order as `view_ids`. Each entry is
            either the `get_view_tickets` payload or, if that view failed,
            `{'view_id': id, 'error': <message>}` — one bad view never sinks
            the rest of the batch.
        """
        if not view_ids:
            return {'count': 0, 'views': []}

        # Bound the pool so a huge id list can't spawn an unbounded thread count.
        max_workers = min(len(view_ids), 10)

        def fetch_one(view_id: int) -> Dict[str, Any]:
            try:
                return self.get_view_tickets(view_id=view_id, limit=limit)
            except Exception as e:
                return {'view_id': view_id, 'error': str(e)}

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            results = list(executor.map(fetch_one, view_ids))

        return {'count': len(results), 'views': results}

    def list_views(self, active_only: bool = True) -> List[Dict[str, Any]]:
        """
        Return all views (filters/queues) visible to the API user, with
        their ids and titles. Useful for resolving a view name to an id
        without leaving the agent.

        `active_only=True` calls zenpy's `views.active()` which hits
        `/api/v2/views/active.json`. Passing `active=True` as a kwarg to
        `views()` does NOT filter — it goes through __call__ to the plain
        `/views.json` endpoint and the kwarg is dropped.
        """
        try:
            views_iter = self.client.views.active() if active_only else self.client.views()
            result = []
            for v in views_iter:
                result.append({
                    'id': v.id,
                    'title': getattr(v, 'title', None),
                    'description': getattr(v, 'description', None),
                    'active': getattr(v, 'active', None),
                    'position': getattr(v, 'position', None),
                })
            return result
        except Exception as e:
            raise Exception(f"Failed to list views: {str(e)}")

    def list_ticket_fields(self) -> List[Dict[str, Any]]:
        """
        Return all ticket field definitions in the workspace, including
        custom fields. Used by clients to resolve custom_field ids -> names.
        """
        try:
            fields = self.client.ticket_fields()
            result = []
            for f in fields:
                result.append({
                    'id': f.id,
                    'title': getattr(f, 'title', None),
                    'type': getattr(f, 'type', None),
                    'description': getattr(f, 'description', None),
                    'active': getattr(f, 'active', None),
                    'tag': getattr(f, 'tag', None),
                })
            return result
        except Exception as e:
            raise Exception(f"Failed to list ticket fields: {str(e)}")

    def get_ticket_comments(self, ticket_id: int) -> List[Dict[str, Any]]:
        """
        Get all comments for a specific ticket, including attachment metadata.

        Only the plain-text `body` is returned. `html_body` is intentionally
        omitted: it carries the same content wrapped in markup, so it roughly
        doubles the payload (and long threads are a common cause of the 1 MB
        result cap) without adding information an agent needs.
        """
        try:
            comments = self.client.tickets.comments(ticket=ticket_id)
            result = []
            for comment in comments:
                attachments = []
                for a in getattr(comment, 'attachments', []) or []:
                    attachments.append({
                        'id': a.id,
                        'file_name': a.file_name,
                        'content_url': a.content_url,
                        'content_type': a.content_type,
                        'size': a.size,
                    })
                result.append({
                    'id': comment.id,
                    'author_id': comment.author_id,
                    'body': comment.body,
                    'public': comment.public,
                    'created_at': str(comment.created_at),
                    'attachments': attachments,
                })
            return result
        except Exception as e:
            raise Exception(f"Failed to get comments for ticket {ticket_id}: {str(e)}")

    def get_ticket_comments_batch(self, ticket_ids: List[int]) -> Dict[str, Any]:
        """
        Fetch comments for several tickets in one call, in parallel.

        Searching Zendesk for trends means reading what people wrote across many
        tickets at once; doing that one `get_ticket_comments` call at a time is
        slow. As with `get_views_batch`, zenpy is blocking, so we fan the
        per-ticket fetches out across a thread pool — wall-clock cost is roughly
        the slowest single ticket rather than their sum.

        Args:
            ticket_ids: Zendesk ticket ids to fetch comments for.

        Returns:
            Dict with `count` (tickets requested) and `tickets`, a list of
            per-ticket results in the same order as `ticket_ids`. Each entry is
            either `{'ticket_id': id, 'comments': [...]}` (same comment shape as
            `get_ticket_comments`) or `{'ticket_id': id, 'error': msg}` if that
            ticket failed — one bad ticket never sinks the batch.
        """
        if not ticket_ids:
            return {'count': 0, 'tickets': []}

        max_workers = min(len(ticket_ids), 10)

        def fetch_one(ticket_id: int) -> Dict[str, Any]:
            try:
                return {'ticket_id': ticket_id, 'comments': self.get_ticket_comments(ticket_id)}
            except Exception as e:
                return {'ticket_id': ticket_id, 'error': str(e)}

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            results = list(executor.map(fetch_one, ticket_ids))

        return {'count': len(results), 'tickets': results}

    # Allowed MIME types. Two groups:
    #   1. Safe images (no SVG — it can carry XML/JS).
    #   2. ZIP-shaped binary bundles. Notability .ntb files are zip archives; logs.zip is zip.
    #      We still enforce ZIP magic bytes on these so the allowlist isn't a blank cheque
    #      for arbitrary binaries — the caller must really be getting a zip.
    _ALLOWED_IMAGE_TYPES = {'image/jpeg', 'image/png', 'image/gif', 'image/webp'}
    _ALLOWED_ZIP_TYPES = {
        'application/zip',
        'application/x-zip-compressed',
        'application/octet-stream',  # Zendesk's default for .ntb
        'application/binary',         # observed verbatim on .ntb attachments via Zendesk CDN
    }
    _ALLOWED_TYPES = _ALLOWED_IMAGE_TYPES | _ALLOWED_ZIP_TYPES

    # Magic bytes (file signatures) for each allowed type.
    # ZIP-shaped types share the standard ZIP local-file-header / end-of-central-directory magics.
    _ZIP_MAGIC_BYTES = [
        b'PK\x03\x04',  # local file header — every non-empty zip starts with this
        b'PK\x05\x06',  # end of central directory record for empty zips
        b'PK\x07\x08',  # spanned-archive marker (rare)
    ]
    _MAGIC_BYTES: Dict[str, List[bytes]] = {
        'image/jpeg': [b'\xff\xd8\xff'],
        'image/png':  [b'\x89PNG\r\n\x1a\n'],
        'image/gif':  [b'GIF87a', b'GIF89a'],
        'image/webp': [b'RIFF'],  # RIFF....WEBP — checked further below
        'application/zip':              _ZIP_MAGIC_BYTES,
        'application/x-zip-compressed': _ZIP_MAGIC_BYTES,
        'application/octet-stream':     _ZIP_MAGIC_BYTES,
        'application/binary':           _ZIP_MAGIC_BYTES,
    }

    # 25 MB hard cap. Images stay token-budget-safe; .ntb files (Notability note bundles)
    # routinely run 5-20 MB when they carry imported PDFs.
    _MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024

    _ATTACHMENT_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})

    @staticmethod
    def _attachment_url_parts(url: str) -> tuple[str, str]:
        """Return (scheme, host), lowercased, parsed the SAME way requests/urllib3
        will CONNECT — deliberately NOT urllib.parse.

        The two parsers disagree on hosts urllib.parse mis-attributes: e.g.
        ``https://evil.com\\@ok.zendesk.com/`` has hostname ``ok.zendesk.com`` under
        urllib.parse but urllib3/requests connects to ``evil.com``. Validating the
        host with a different parser than the one that opens the socket is an
        allowlist bypass, so we parse with urllib3 here. Returns ("", "") if the
        URL is unparseable (→ rejected)."""
        try:
            parsed = _parse_url(url)
        except Exception:
            return "", ""
        return (parsed.scheme or "").lower(), (parsed.host or "").lower()

    @staticmethod
    def _is_zendesk_owned(host: str) -> bool:
        return (
            host == "zendesk.com" or host.endswith(".zendesk.com")
            or host == "zdusercontent.com" or host.endswith(".zdusercontent.com")
        )

    def _attachment_host_allowed(self, url: str, *, with_auth: bool) -> bool:
        """Whether we may fetch ``url``. Always requires https + a Zendesk-owned
        host. When ``with_auth`` (the Zendesk credential will be attached) the host
        must be THIS tenant's own subdomain — the only place this account's
        content_urls live and the only host the credential should ever reach.
        Credential-free hops (after a cross-host redirect) may also touch the CDN
        or other Zendesk hosts, keeping SSRF contained to Zendesk infrastructure."""
        scheme, host = self._attachment_url_parts(url)
        if scheme != "https" or not host:
            return False
        if with_auth:
            return host == f"{self.subdomain}.zendesk.com".lower()
        return self._is_zendesk_owned(host)

    def _fetch_attachment_response(self, url: str, max_hops: int = 5):
        """GET an attachment, following redirects MANUALLY.

        Every hop is re-validated BEFORE the request, using the same parser that
        opens the socket (closing parser-differential allowlist bypasses); the
        host must be https and Zendesk-owned; and the Zendesk credential is sent
        ONLY to this tenant's own subdomain — dropped on the first cross-host hop
        and never reattached. Returns the final streamed response.
        """
        current = url
        send_auth = True
        for _ in range(max_hops + 1):
            if not self._attachment_host_allowed(current, with_auth=send_auth):
                scheme, host = self._attachment_url_parts(current)
                if scheme != "https":
                    raise ValueError(
                        f"Refusing to fetch attachment over non-https URL (host '{host}')."
                    )
                if send_auth and self._is_zendesk_owned(host):
                    raise ValueError(
                        f"Refusing to send Zendesk credentials to foreign host '{host}' "
                        f"(expected {self.subdomain}.zendesk.com)."
                    )
                raise ValueError(
                    f"Refusing to fetch attachment from non-Zendesk host '{host}'."
                )
            headers = {'Authorization': self.auth_header} if send_auth else {}
            response = _requests.get(
                current, headers=headers, timeout=30, stream=True, allow_redirects=False
            )
            if response.status_code in self._ATTACHMENT_REDIRECT_CODES:
                location = response.headers.get('Location')
                response.close()
                if not location:
                    raise ValueError("Attachment redirect response had no Location header.")
                nxt = urllib.parse.urljoin(current, location)
                _, cur_host = self._attachment_url_parts(current)
                _, nxt_host = self._attachment_url_parts(nxt)
                if nxt_host != cur_host:
                    send_auth = False  # never send our Zendesk credential to a different host
                current = nxt
                continue
            return response
        raise ValueError("Too many redirects while fetching attachment.")

    def get_ticket_attachment(self, content_url: str) -> Dict[str, Any]:
        """
        Fetch an attachment and return base64-encoded data.

        Security measures applied:
        - Allowlist of MIME types: safe images plus ZIP-shaped binary bundles
          (Notability .ntb files are zip archives; logs.zip is zip).
        - Magic byte validation so the file header must match the declared type.
          For ZIP-shaped allowlisted MIME types, we enforce the standard ZIP signature,
          so the binary allowlist is not a blank cheque for arbitrary content.
        - 25 MB size cap to prevent image bombs and excessive token usage.

        content_url originates from ticket/comment data, which is attacker-
        influenced (a malicious ticket could plant a bogus content_url), so a
        prompt-injection could try to coax the model into leaking our Zendesk
        credential. The fetch (see _fetch_attachment_response) defends against
        that: it is https-only, validates every hop's host with the SAME parser
        that opens the socket, sends the credential ONLY to this tenant's own
        subdomain, and refuses any non-Zendesk host — no redirect can launder the
        request off Zendesk infrastructure or onto an attacker's host.
        """
        try:
            response = self._fetch_attachment_response(content_url)
            response.raise_for_status()

            content_type = response.headers.get('Content-Type', '').split(';')[0].strip().lower()

            if content_type not in self._ALLOWED_TYPES:
                raise ValueError(
                    f"Attachment type '{content_type}' is not allowed. "
                    f"Supported types: {sorted(self._ALLOWED_TYPES)}"
                )

            # Fail fast on declared oversize before buffering anything.
            declared_len_raw = response.headers.get('Content-Length')
            declared_len = None
            if declared_len_raw is not None and declared_len_raw.isdigit():
                declared_len = int(declared_len_raw)
            if declared_len is not None and declared_len > self._MAX_ATTACHMENT_BYTES:
                raise ValueError(
                    f"Attachment exceeds the {self._MAX_ATTACHMENT_BYTES // (1024*1024)} MB size limit."
                )

            # Read with size cap — stops download as soon as limit is exceeded.
            chunks = []
            total = 0
            for chunk in response.iter_content(chunk_size=65536):
                total += len(chunk)
                if total > self._MAX_ATTACHMENT_BYTES:
                    raise ValueError(
                        f"Attachment exceeds the {self._MAX_ATTACHMENT_BYTES // (1024*1024)} MB size limit."
                    )
                chunks.append(chunk)
            content = b''.join(chunks)

            # Validate magic bytes to catch MIME type spoofing.
            magic_signatures = self._MAGIC_BYTES.get(content_type, [])
            if magic_signatures and not any(content.startswith(sig) for sig in magic_signatures):
                raise ValueError(
                    f"File header does not match declared content type '{content_type}'. "
                    "The attachment may be spoofed."
                )
            # Extra check for WebP: bytes 8–12 must be b'WEBP'.
            if content_type == 'image/webp' and content[8:12] != b'WEBP':
                raise ValueError("File header does not match declared content type 'image/webp'.")

            return {
                'data': base64.b64encode(content).decode('ascii'),
                'content_type': content_type,
            }
        except (ValueError, _requests.HTTPError):
            raise
        except Exception as e:
            raise Exception(f"Failed to fetch attachment from {content_url}: {str(e)}")

    def post_comment(self, ticket_id: int, comment: str, public: bool = True) -> str:
        """
        Post a comment to an existing ticket.
        """
        try:
            ticket = self.client.tickets(id=ticket_id)
            ticket.comment = Comment(
                html_body=comment,
                public=public
            )
            self.client.tickets.update(ticket)
            return comment
        except Exception as e:
            raise Exception(f"Failed to post comment on ticket {ticket_id}: {str(e)}")

    def get_tickets(self, page: int = 1, per_page: int = 25, sort_by: str = 'created_at', sort_order: str = 'desc') -> Dict[str, Any]:
        """
        Get the latest tickets with proper pagination support using direct API calls.

        Args:
            page: Page number (1-based)
            per_page: Number of tickets per page (max 100)
            sort_by: Field to sort by (created_at, updated_at, priority, status)
            sort_order: Sort order (asc or desc)

        Returns:
            Dict containing tickets and pagination info
        """
        try:
            # Cap at reasonable limit
            per_page = min(per_page, 100)

            # Build URL with parameters for offset pagination
            params = {
                'page': str(page),
                'per_page': str(per_page),
                'sort_by': sort_by,
                'sort_order': sort_order
            }
            query_string = urllib.parse.urlencode(params)
            url = f"{self.base_url}/tickets.json?{query_string}"

            # Create request with auth header
            req = urllib.request.Request(url)
            req.add_header('Authorization', self.auth_header)
            req.add_header('Content-Type', 'application/json')

            # Make the API request
            with urllib.request.urlopen(req) as response:
                data = json.loads(response.read().decode())

            tickets_data = data.get('tickets', [])

            # Process tickets to return only essential fields
            ticket_list = []
            for ticket in tickets_data:
                ticket_list.append({
                    'id': ticket.get('id'),
                    'subject': ticket.get('subject'),
                    'status': ticket.get('status'),
                    'priority': ticket.get('priority'),
                    'description': ticket.get('description'),
                    'created_at': ticket.get('created_at'),
                    'updated_at': ticket.get('updated_at'),
                    'requester_id': ticket.get('requester_id'),
                    'assignee_id': ticket.get('assignee_id')
                })

            return {
                'tickets': ticket_list,
                'page': page,
                'per_page': per_page,
                'count': len(ticket_list),
                'sort_by': sort_by,
                'sort_order': sort_order,
                'has_more': data.get('next_page') is not None,
                'next_page': page + 1 if data.get('next_page') else None,
                'previous_page': page - 1 if data.get('previous_page') and page > 1 else None
            }
        except urllib.error.HTTPError as e:
            error_body = e.read().decode() if e.fp else "No response body"
            raise Exception(f"Failed to get latest tickets: HTTP {e.code} - {e.reason}. {error_body}")
        except Exception as e:
            raise Exception(f"Failed to get latest tickets: {str(e)}")

    def search(
        self,
        query: str,
        type: str | None = None,
        sort_by: str | None = None,
        sort_order: str | None = None,
        page: int = 1,
        per_page: int = 25,
    ) -> Dict[str, Any]:
        """
        Full-text search across Zendesk using the Search API
        (/api/v2/search.json). Unlike views, this searches ticket bodies,
        comments, subjects, tags, and more.

        Args:
            query: Zendesk search query string. Supports full-text terms and
                field qualifiers, e.g. 'crash type:ticket status:open'.
                See https://support.zendesk.com/hc/en-us/articles/4408886879258
            type: Optional result type to restrict to (ticket, user,
                organization, group). Prepended as 'type:<type>' if the query
                does not already specify a type.
            sort_by: Optional field to sort by (e.g. created_at, updated_at,
                priority, status, ticket_type).
            sort_order: Optional sort order (asc or desc).
            page: Page number (1-based).
            per_page: Results per page (max 100).

        Returns:
            Dict containing results and pagination info.
        """
        try:
            per_page = min(per_page, 100)

            full_query = query
            if type and f"type:{type}" not in query:
                full_query = f"type:{type} {query}".strip()

            params = {
                'query': full_query,
                'page': str(page),
                'per_page': str(per_page),
            }
            if sort_by:
                params['sort_by'] = sort_by
            if sort_order:
                params['sort_order'] = sort_order

            query_string = urllib.parse.urlencode(params)
            url = f"{self.base_url}/search.json?{query_string}"

            req = urllib.request.Request(url)
            req.add_header('Authorization', self.auth_header)
            req.add_header('Content-Type', 'application/json')

            with urllib.request.urlopen(req) as response:
                data = json.loads(response.read().decode())

            results = [self._trim_search_result(r) for r in data.get('results', [])]
            return {
                'results': results,
                'count': data.get('count', 0),
                'page': page,
                'per_page': per_page,
                'has_more': data.get('next_page') is not None,
                'next_page': page + 1 if data.get('next_page') else None,
                'previous_page': page - 1 if data.get('previous_page') and page > 1 else None,
            }
        except urllib.error.HTTPError as e:
            error_body = e.read().decode() if e.fp else "No response body"
            raise Exception(f"Failed to search: HTTP {e.code} - {e.reason}. {error_body}")
        except Exception as e:
            raise Exception(f"Failed to search: {str(e)}")

    @staticmethod
    def _trim_search_result(r: Dict[str, Any]) -> Dict[str, Any]:
        """
        Trim a raw Search API result to a summary.

        Search returns full objects, and a ticket carries its entire
        `description` (plus a wide custom_fields/fields block) — at per_page=100
        that easily blows the 1 MB result cap. We keep the fields needed to
        identify and triage a result and drop the bulky body; callers fetch full
        detail via get_ticket / get_users when they need it. Unknown result
        types fall back to a minimal id + result_type so nothing silently
        vanishes.
        """
        rtype = r.get('result_type')
        if rtype == 'ticket':
            return {
                'result_type': 'ticket',
                'id': r.get('id'),
                'subject': r.get('subject'),
                'status': r.get('status'),
                'priority': r.get('priority'),
                'type': r.get('type'),
                'created_at': r.get('created_at'),
                'updated_at': r.get('updated_at'),
                'requester_id': r.get('requester_id'),
                'assignee_id': r.get('assignee_id'),
                'organization_id': r.get('organization_id'),
                'tags': r.get('tags', []),
            }
        if rtype == 'user':
            return {
                'result_type': 'user',
                'id': r.get('id'),
                'name': r.get('name'),
                'email': r.get('email'),
                'role': r.get('role'),
                'active': r.get('active'),
                'organization_id': r.get('organization_id'),
            }
        if rtype in ('organization', 'group'):
            return {
                'result_type': rtype,
                'id': r.get('id'),
                'name': r.get('name'),
            }
        return {'result_type': rtype, 'id': r.get('id')}

    def search_ticket_comments(
        self,
        text: str,
        sort_by: str | None = None,
        sort_order: str | None = None,
        page: int = 1,
        per_page: int = 25,
    ) -> Dict[str, Any]:
        """
        Keyword search over the text users actually wrote — ticket subject,
        description, and comment bodies.

        This is the generic `search` scoped to `type:ticket`. It exists as its
        own method (and tool) so the keyword is searched against ticket content
        rather than being read as Zendesk query syntax: a bare phrase like
        `cannot export pdf` is passed straight through to the Search API, which
        full-text matches it against ticket text. Use plain words/quoted phrases
        here; reach for `search` when you need field qualifiers (status:open,
        requester:..., created>...).

        Args:
            text: Words or quoted phrase to find in ticket text.
            sort_by: Optional field to sort by (created_at, updated_at, etc.).
            sort_order: Optional sort order (asc or desc).
            page: Page number (1-based).
            per_page: Results per page (max 100).

        Returns:
            Same shape as `search` — trimmed ticket summaries plus pagination.
        """
        return self.search(
            query=text,
            type='ticket',
            sort_by=sort_by,
            sort_order=sort_order,
            page=page,
            per_page=per_page,
        )

    def get_satisfaction_ratings(
        self,
        score: str | None = None,
        start_time: int | None = None,
        end_time: int | None = None,
        page: int = 1,
        per_page: int = 100,
    ) -> Dict[str, Any]:
        """
        List CSAT / satisfaction ratings via the Satisfaction Ratings API
        (/api/v2/satisfaction_ratings.json).

        The list endpoint has no server-side agent filter, so per-agent CSAT
        analysis is done client-side off the `assignee_id` on each rating.
        This method just surfaces the raw ratings (and their assignee_id,
        score, comment, ticket_id, timestamps) plus pagination so the caller
        can group/rank by agent and date range.

        Args:
            score: Optional score filter. Zendesk accepts: offered, unoffered,
                received, received_with_comment, received_without_comment,
                good, good_with_comment, good_without_comment, bad,
                bad_with_comment, bad_without_comment.
            start_time: Optional Unix epoch (seconds) — only ratings created
                at/after this time.
            end_time: Optional Unix epoch (seconds) — only ratings created
                at/before this time.
            page: Page number (1-based).
            per_page: Results per page (max 100).

        Returns:
            Dict with `satisfaction_ratings` (list of raw rating objects),
            `count`, and pagination info.
        """
        try:
            per_page = min(per_page, 100)

            params = {
                'page': str(page),
                'per_page': str(per_page),
            }
            if score:
                params['score'] = score
            if start_time is not None:
                params['start_time'] = str(start_time)
            if end_time is not None:
                params['end_time'] = str(end_time)

            query_string = urllib.parse.urlencode(params)
            url = f"{self.base_url}/satisfaction_ratings.json?{query_string}"

            req = urllib.request.Request(url)
            req.add_header('Authorization', self.auth_header)
            req.add_header('Content-Type', 'application/json')

            with urllib.request.urlopen(req) as response:
                data = json.loads(response.read().decode())

            ratings = [{
                'id': r.get('id'),
                'assignee_id': r.get('assignee_id'),
                'requester_id': r.get('requester_id'),
                'group_id': r.get('group_id'),
                'ticket_id': r.get('ticket_id'),
                'score': r.get('score'),
                'comment': r.get('comment'),
                'reason': r.get('reason'),
                'created_at': r.get('created_at'),
                'updated_at': r.get('updated_at'),
            } for r in data.get('satisfaction_ratings', [])]
            return {
                'satisfaction_ratings': ratings,
                'count': data.get('count', 0),
                'page': page,
                'per_page': per_page,
                'has_more': data.get('next_page') is not None,
                'next_page': page + 1 if data.get('next_page') else None,
                'previous_page': page - 1 if data.get('previous_page') and page > 1 else None,
            }
        except urllib.error.HTTPError as e:
            error_body = e.read().decode() if e.fp else "No response body"
            raise Exception(f"Failed to get satisfaction ratings: HTTP {e.code} - {e.reason}. {error_body}")
        except Exception as e:
            raise Exception(f"Failed to get satisfaction ratings: {str(e)}")

    def get_ticket_metrics(self, ticket_id: int) -> Dict[str, Any]:
        """
        Fetch the metric set for a single ticket via the Ticket Metrics API
        (/api/v2/tickets/{id}/metrics.json).

        Views and the ticket object itself expose status and timestamps, but
        not the *durations* support teams actually report on: how long until
        the first agent reply, how long until full resolution, and how long the
        ticket sat in each state. Those live only on the metric set. Surfacing
        them lets a dashboard compute KPIs like average first reply time or
        resolution time per agent — numbers you cannot derive from views alone.

        Zendesk reports each duration twice: in calendar minutes and in
        business (schedule) minutes. Both are passed through untouched.

        Args:
            ticket_id: The ticket whose metrics to fetch.

        Returns:
            The raw `ticket_metric` object, including reply_time_in_minutes,
            first_resolution_time_in_minutes, full_resolution_time_in_minutes,
            and the various *_breaches / *_at fields, plus created/updated
            timestamps.
        """
        try:
            url = f"{self.base_url}/tickets/{ticket_id}/metrics.json"

            req = urllib.request.Request(url)
            req.add_header('Authorization', self.auth_header)
            req.add_header('Content-Type', 'application/json')

            with urllib.request.urlopen(req) as response:
                data = json.loads(response.read().decode())

            return data.get('ticket_metric', data)
        except urllib.error.HTTPError as e:
            error_body = e.read().decode() if e.fp else "No response body"
            raise Exception(f"Failed to get metrics for ticket {ticket_id}: HTTP {e.code} - {e.reason}. {error_body}")
        except Exception as e:
            raise Exception(f"Failed to get metrics for ticket {ticket_id}: {str(e)}")

    def get_users(self, user_ids: List[int]) -> Dict[str, Any]:
        """
        Resolve one or more user ids to their profiles via the Show Many Users
        API (/api/v2/users/show_many.json?ids=...).

        Tickets reference people only by numeric id (requester_id,
        assignee_id), so raw ticket data can't answer "who is waiting" or
        "which agent owns this" without a name/email lookup. This resolves a
        batch of ids in a single round trip rather than one request per user.

        Args:
            user_ids: List of Zendesk user ids to resolve (max 100 per call —
                the Show Many endpoint's hard limit).

        Returns:
            Dict with `count` and `users`, a list of trimmed profiles
            (id, name, email, role, active, organization_id, time_zone,
            created_at, updated_at) in Zendesk's returned order.
        """
        try:
            if not user_ids:
                return {'count': 0, 'users': []}

            # Show Many caps at 100 ids per request. Fail loudly rather than
            # silently truncating — a partial result that looks complete is a
            # correctness trap for callers resolving a whole queue.
            if len(user_ids) > 100:
                raise ValueError(
                    f"get_users accepts at most 100 ids per call, got {len(user_ids)}"
                )

            ids_param = ','.join(str(uid) for uid in user_ids)
            params = urllib.parse.urlencode({'ids': ids_param})
            url = f"{self.base_url}/users/show_many.json?{params}"

            req = urllib.request.Request(url)
            req.add_header('Authorization', self.auth_header)
            req.add_header('Content-Type', 'application/json')

            with urllib.request.urlopen(req) as response:
                data = json.loads(response.read().decode())

            users = [{
                'id': u.get('id'),
                'name': u.get('name'),
                'email': u.get('email'),
                'role': u.get('role'),
                'active': u.get('active'),
                'organization_id': u.get('organization_id'),
                'time_zone': u.get('time_zone'),
                'created_at': u.get('created_at'),
                'updated_at': u.get('updated_at'),
            } for u in data.get('users', [])]

            return {'count': len(users), 'users': users}
        except urllib.error.HTTPError as e:
            error_body = e.read().decode() if e.fp else "No response body"
            raise Exception(f"Failed to get users {user_ids}: HTTP {e.code} - {e.reason}. {error_body}")
        except Exception as e:
            raise Exception(f"Failed to get users {user_ids}: {str(e)}")

    def get_ticket_counts_by_status(
        self,
        assignee_id: int | None = None,
        statuses: List[str] | None = None,
    ) -> Dict[str, Any]:
        """
        Return per-status ticket counts in one call, optionally scoped to a
        single agent, via the Search Count API (/api/v2/search/count.json).

        A dashboard that wants "open + pending + on-hold for this agent"
        otherwise needs a separate view (or full ticket fetch) per status. The
        Search Count endpoint returns just an integer per query, so we fan the
        per-status queries out across a thread pool and assemble one map. This
        is far cheaper than pulling ticket bodies only to count them.

        Args:
            assignee_id: Optional agent id to scope the counts to. Omit for a
                workspace-wide count.
            statuses: Optional list of statuses to count. Defaults to the
                active workload (new, open, pending, hold). Valid values are
                new, open, pending, hold, solved, closed; an unknown status
                raises. Zendesk's "on-hold" status is queried as `hold`.

        Returns:
            Dict with `assignee_id`, `counts` (status -> `{'count': int}`, or
            `{'error': msg}` if that status query failed — one bad query never
            sinks the rest), and `total` (sum across statuses that succeeded).
        """
        valid_statuses = {'new', 'open', 'pending', 'hold', 'solved', 'closed'}
        try:
            if statuses is None:
                statuses = ['new', 'open', 'pending', 'hold']

            unknown = [s for s in statuses if s not in valid_statuses]
            if unknown:
                raise ValueError(
                    f"Unknown status(es) {unknown}; valid: {sorted(valid_statuses)}"
                )

            def count_one(status: str) -> Dict[str, Any]:
                try:
                    query = f"type:ticket status:{status}"
                    if assignee_id is not None:
                        query += f" assignee:{assignee_id}"
                    params = urllib.parse.urlencode({'query': query})
                    url = f"{self.base_url}/search/count.json?{params}"
                    req = urllib.request.Request(url)
                    req.add_header('Authorization', self.auth_header)
                    req.add_header('Content-Type', 'application/json')
                    with urllib.request.urlopen(req) as response:
                        data = json.loads(response.read().decode())
                    return {'count': int(data.get('count', 0))}
                except Exception as e:
                    # One bad status query never sinks the rest of the batch.
                    return {'error': str(e)}

            max_workers = min(len(statuses), 10) if statuses else 1
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                results = list(executor.map(count_one, statuses))

            counts = dict(zip(statuses, results))
            total = sum(r['count'] for r in results if 'count' in r)
            return {
                'assignee_id': assignee_id,
                'counts': counts,
                'total': total,
            }
        except urllib.error.HTTPError as e:
            error_body = e.read().decode() if e.fp else "No response body"
            raise Exception(f"Failed to get ticket counts: HTTP {e.code} - {e.reason}. {error_body}")
        except Exception as e:
            raise Exception(f"Failed to get ticket counts: {str(e)}")

    def get_all_articles(self) -> Dict[str, Any]:
        """
        Fetch help center articles as knowledge base.
        Returns a Dict of section -> [article].
        """
        try:
            # Get all sections
            sections = self.client.help_center.sections()

            # Get articles for each section
            kb = {}
            for section in sections:
                articles = self.client.help_center.sections.articles(section.id)
                kb[section.name] = {
                    'section_id': section.id,
                    'description': section.description,
                    'articles': [{
                        'id': article.id,
                        'title': article.title,
                        'body': article.body,
                        'updated_at': str(article.updated_at),
                        'url': article.html_url
                    } for article in articles]
                }

            return kb
        except Exception as e:
            raise Exception(f"Failed to fetch knowledge base: {str(e)}")

    def create_ticket(
        self,
        subject: str,
        description: str,
        requester_id: int | None = None,
        assignee_id: int | None = None,
        priority: str | None = None,
        type: str | None = None,
        tags: List[str] | None = None,
        custom_fields: List[Dict[str, Any]] | None = None,
    ) -> Dict[str, Any]:
        """
        Create a new Zendesk ticket using Zenpy and return essential fields.

        Args:
            subject: Ticket subject
            description: Ticket description (plain text). Will also be used as initial comment.
            requester_id: Optional requester user ID
            assignee_id: Optional assignee user ID
            priority: Optional priority (low, normal, high, urgent)
            type: Optional ticket type (problem, incident, question, task)
            tags: Optional list of tags
            custom_fields: Optional list of dicts: {id: int, value: Any}
        """
        try:
            ticket = ZenpyTicket(
                subject=subject,
                description=description,
                requester_id=requester_id,
                assignee_id=assignee_id,
                priority=priority,
                type=type,
                tags=tags,
                custom_fields=custom_fields,
            )
            created_audit = self.client.tickets.create(ticket)
            # Fetch created ticket id from audit
            created_ticket_id = getattr(getattr(created_audit, 'ticket', None), 'id', None)
            if created_ticket_id is None:
                # Fallback: try to read id from audit events
                created_ticket_id = getattr(created_audit, 'id', None)

            # Fetch full ticket to return consistent data
            created = self.client.tickets(id=created_ticket_id) if created_ticket_id else None

            return {
                'id': getattr(created, 'id', created_ticket_id),
                'subject': getattr(created, 'subject', subject),
                'description': getattr(created, 'description', description),
                'status': getattr(created, 'status', 'new'),
                'priority': getattr(created, 'priority', priority),
                'type': getattr(created, 'type', type),
                'created_at': str(getattr(created, 'created_at', '')),
                'updated_at': str(getattr(created, 'updated_at', '')),
                'requester_id': getattr(created, 'requester_id', requester_id),
                'assignee_id': getattr(created, 'assignee_id', assignee_id),
                'organization_id': getattr(created, 'organization_id', None),
                'tags': list(getattr(created, 'tags', tags or []) or []),
            }
        except Exception as e:
            raise Exception(f"Failed to create ticket: {str(e)}")

    def update_ticket(self, ticket_id: int, **fields: Any) -> Dict[str, Any]:
        """
        Update a Zendesk ticket with provided fields using Zenpy.

        Supported fields include common ticket attributes like:
        subject, status, priority, type, assignee_id, requester_id,
        tags (list[str]), custom_fields (list[dict]), due_at, etc.
        """
        try:
            # Load the ticket, mutate fields directly, and update
            ticket = self.client.tickets(id=ticket_id)
            for key, value in fields.items():
                if value is None:
                    continue
                setattr(ticket, key, value)

            # This call returns a TicketAudit (not a Ticket). Don't read attrs from it.
            self.client.tickets.update(ticket)

            # Fetch the fresh ticket to return consistent data
            refreshed = self.client.tickets(id=ticket_id)

            return {
                'id': refreshed.id,
                'subject': refreshed.subject,
                'description': refreshed.description,
                'status': refreshed.status,
                'priority': refreshed.priority,
                'type': getattr(refreshed, 'type', None),
                'created_at': str(refreshed.created_at),
                'updated_at': str(refreshed.updated_at),
                'requester_id': refreshed.requester_id,
                'assignee_id': refreshed.assignee_id,
                'organization_id': refreshed.organization_id,
                'tags': list(getattr(refreshed, 'tags', []) or []),
            }
        except Exception as e:
            raise Exception(f"Failed to update ticket {ticket_id}: {str(e)}")