"""One-time HTTP links for moving file bytes past the model's context.

MCP tool arguments and results pass through the model, which makes them a poor
channel for whole PDFs. When the server runs over Streamable HTTP behind a
public URL (``REMARKABLE_PUBLIC_URL``), two plain HTTP routes let a client's
code sandbox move the bytes directly:

- ``PUT/POST /files/upload/{token}``: ``remarkable_upload_link`` hands out a
  single-use, expiring token bound to the upload options chosen in the tool
  call. The request body is the raw PDF/EPUB; the response is the upload result.
- ``GET /files/export/{export_id}/{filename}``: downloads a temporary export
  created by ``remarkable_export``. The 128-bit random export id is the
  capability, exactly like the ``remarkableexport://`` resource URI.

These routes bypass MCP authorization (they are Starlette custom routes), so a
reverse proxy in front of the server must let ``/files/`` through without its
own login. Possession of a token or export id is the authorization. Without
``REMARKABLE_PUBLIC_URL`` nothing here is registered.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, List, Literal, Optional
from urllib.parse import quote

UPLOAD_LINK_TTL_SECONDS = 15 * 60
UPLOAD_LINK_MAX_PENDING = 32


def public_base_url() -> Optional[str]:
    """Return the configured public base URL without a trailing slash, or None."""
    url = os.environ.get("REMARKABLE_PUBLIC_URL", "").strip().rstrip("/")
    if not url:
        return None
    if not url.startswith(("https://", "http://")):
        return None
    return url


@dataclass
class UploadRequest:
    """Upload options bound to a one-time token."""

    document_name: str
    parent_folder: str = "/"
    file_type: str = "pdf"
    tags: List[str] = field(default_factory=list)
    replace: bool = False
    force_replace: bool = False
    create_folders: bool = False


@dataclass
class _PendingUpload:
    request: UploadRequest
    expires_monotonic: float
    expires_at: datetime
    in_use: bool = False


class UploadLinkStore:
    """Bounded, expiring, single-use upload tokens."""

    def __init__(
        self,
        *,
        ttl_seconds: int = UPLOAD_LINK_TTL_SECONDS,
        max_pending: int = UPLOAD_LINK_MAX_PENDING,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ):
        self.ttl_seconds = ttl_seconds
        self.max_pending = max_pending
        self._clock = monotonic_clock
        self._pending: dict[str, _PendingUpload] = {}
        self._lock = threading.Lock()

    def _prune_locked(self, now: float) -> None:
        for token in [t for t, p in self._pending.items() if now >= p.expires_monotonic]:
            del self._pending[token]

    def create(self, request: UploadRequest) -> tuple[str, datetime]:
        """Register ``request`` and return ``(token, expires_at)``."""
        now = self._clock()
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=self.ttl_seconds)
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._prune_locked(now)
            while len(self._pending) >= self.max_pending:
                oldest = min(self._pending, key=lambda t: self._pending[t].expires_monotonic)
                del self._pending[oldest]
            self._pending[token] = _PendingUpload(
                request=request,
                expires_monotonic=now + self.ttl_seconds,
                expires_at=expires_at,
            )
        return token, expires_at

    def claim(self, token: str) -> Optional[UploadRequest]:
        """Reserve a live token for one upload attempt; None if unknown/expired/busy."""
        with self._lock:
            self._prune_locked(self._clock())
            pending = self._pending.get(token)
            if pending is None or pending.in_use:
                return None
            pending.in_use = True
            return pending.request

    def release(self, token: str, *, consumed: bool) -> None:
        """Finish an attempt: drop the token when consumed, else allow a retry."""
        with self._lock:
            if consumed:
                self._pending.pop(token, None)
            elif token in self._pending:
                self._pending[token].in_use = False


upload_links = UploadLinkStore()


def export_download_url(export_id: str, filename: str) -> Optional[str]:
    """Public download URL for a temporary export, when a public URL is configured."""
    base = public_base_url()
    if base is None:
        return None
    return f"{base}/files/export/{export_id}/{quote(filename)}"


def _json_response(payload: str, status_code: int):
    from starlette.responses import Response

    return Response(payload, status_code=status_code, media_type="application/json")


async def _read_limited_body(request, limit: int) -> Optional[bytes]:
    """Read the request body, returning None once it exceeds ``limit`` bytes."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        return None
    chunks = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


async def handle_upload(request):
    """Starlette endpoint for ``PUT/POST /files/upload/{token}``."""
    from remarkable_mcp.responses import make_error
    from remarkable_mcp.write_tools import max_upload_bytes, upload_bytes

    token = request.path_params.get("token", "")
    upload = upload_links.claim(token)
    if upload is None:
        return _json_response(
            make_error(
                error_type="invalid_upload_link",
                message="This upload link is unknown, expired, already used, or busy.",
                suggestion="Request a fresh link with remarkable_upload_link.",
            ),
            404,
        )

    consumed = False
    try:
        data = await _read_limited_body(request, max_upload_bytes())
        if data is None:
            return _json_response(
                make_error(
                    error_type="upload_too_large",
                    message=f"The upload exceeds {max_upload_bytes()} bytes.",
                    suggestion="Raise REMARKABLE_MAX_UPLOAD_MB on the server or send less.",
                ),
                413,
            )
        result = await upload_bytes(
            data,
            upload.file_type,
            upload.document_name,
            parent_folder=upload.parent_folder,
            tags=upload.tags,
            replace=upload.replace,
            force_replace=upload.force_replace,
            create_folders=upload.create_folders,
        )
        failed = "_error" in json.loads(result)
        # Bad bytes can be fixed and resent with the same link; anything that
        # reached the tablet consumes it.
        consumed = not failed or json.loads(result)["_error"]["type"] not in (
            "invalid_file",
            "empty_upload",
            "unsupported_format",
            "upload_too_large",
        )
        return _json_response(result, 400 if failed else 200)
    finally:
        upload_links.release(token, consumed=consumed)


async def handle_export_download(request):
    """Starlette endpoint for ``GET /files/export/{export_id}/{filename}``."""
    from starlette.responses import Response

    from remarkable_mcp.export_resources import export_store
    from remarkable_mcp.responses import make_error

    export_id = request.path_params.get("export_id", "")
    try:
        data, resource = export_store.read_with_resource(export_id)
    except FileNotFoundError:
        return _json_response(
            make_error(
                error_type="export_not_found",
                message="This export is unknown or has expired.",
                suggestion="Run remarkable_export again for a fresh link.",
            ),
            404,
        )
    return Response(
        data,
        media_type=resource.mime_type,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(resource.filename)}",
            "Cache-Control": "no-store",
        },
    )


def register_download_route() -> None:
    """Expose temporary exports over HTTP when a public URL is configured."""
    if public_base_url() is None:
        return
    from remarkable_mcp.server import mcp

    mcp.custom_route(
        "/files/export/{export_id}/{filename}",
        methods=["GET"],
        include_in_schema=False,
    )(handle_export_download)


def register_upload_link_tool() -> None:
    """Register ``remarkable_upload_link`` and its HTTP route (write mode + public URL)."""
    if public_base_url() is None:
        return
    from mcp.types import ToolAnnotations

    from remarkable_mcp.responses import make_error, make_response
    from remarkable_mcp.server import mcp
    from remarkable_mcp.tags import clean_tag_list
    from remarkable_mcp.write_tools import max_upload_bytes

    mcp.custom_route(
        "/files/upload/{token}",
        methods=["PUT", "POST"],
        include_in_schema=False,
    )(handle_upload)

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=False))
    async def remarkable_upload_link(
        document_name: str,
        parent_folder: str = "/",
        file_type: Literal["pdf", "epub"] = "pdf",
        tags: Optional[List[str]] = None,
        replace: bool = False,
        force_replace: bool = False,
        create_folders: bool = False,
    ) -> str:
        """
        <usecase>Get a one-time HTTPS link to upload a PDF/EPUB without passing its bytes
        through the conversation.</usecase>
        <instructions>
        Preferred way to put a file you generated (e.g. in a code sandbox) onto the
        tablet. Call this tool with the target name, folder and options, then send
        the raw file from your sandbox with the returned curl command:

            curl -sS --fail-with-body -T plan.pdf "<upload_url>"

        The upload runs when the bytes arrive; the curl output is the same JSON a
        remarkable_upload call returns (document uuid, path, replaced versions).
        HTTP 200 means the document is on its way to the tablet; 400 means it was
        rejected and the JSON says why.

        The link is valid once, for 15 minutes. If the server rejects the bytes as
        not being a PDF/EPUB, you may resend corrected bytes to the same link.
        Your sandbox must be allowed to reach the server's domain; if the request
        is blocked, fall back to remarkable_upload_base64 for small files.

        Requires write mode (the default; disabled with --read-only).
        </instructions>
        <parameters>
        - document_name: Display name on the tablet (required).
        - parent_folder: Destination folder path (default: root "/").
        - file_type: "pdf" (default) or "epub".
        - tags: Document tags to set, e.g. ["tagesplan"]. Cloud and SSH.
        - replace: Cloud only. Replace a same-named document in the same folder.
          Refuses if that document has handwriting, unless force_replace=True.
          The old version goes to the trash.
        - force_replace: Cloud only. Replace even a document with handwriting.
        - create_folders: Cloud only. Create a missing parent_folder.
        </parameters>
        <examples>
        - remarkable_upload_link("Tagesplan 2026-10-19", parent_folder="/Tagesplan",
                                 tags=["tagesplan"], replace=True, create_folders=True)
          then: curl -sS --fail-with-body -T tagesplan.pdf "<upload_url>"
        </examples>
        """
        from remarkable_mcp.write_tools import _require_write_transport

        error = _require_write_transport()
        if error:
            return error
        name = (document_name or "").strip()
        if not name:
            return make_error(
                error_type="missing_document_name",
                message="document_name is required.",
                suggestion="Pass the display name the document should have on the tablet.",
            )
        token, expires_at = upload_links.create(
            UploadRequest(
                document_name=name,
                parent_folder=parent_folder or "/",
                file_type=file_type,
                tags=clean_tag_list(tags),
                replace=replace,
                force_replace=force_replace,
                create_folders=create_folders,
            )
        )
        url = f"{public_base_url()}/files/upload/{token}"
        return make_response(
            {
                "upload_url": url,
                "method": "PUT (POST also accepted)",
                "body": f"raw {file_type.upper()} bytes, at most {max_upload_bytes()} bytes",
                "curl": f'curl -sS --fail-with-body -T document.{file_type} "{url}"',
                "expires_at": expires_at,
                "single_use": True,
                "document_name": name,
                "parent_folder": parent_folder or "/",
            },
            "Send the file with the curl command from your code sandbox. Its output is "
            "the upload result. The link works once and expires in 15 minutes.",
        )
