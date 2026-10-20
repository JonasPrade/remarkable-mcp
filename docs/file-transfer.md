# File transfer for remote clients

`remarkable_upload` reads a file from the MCP server's own disk. A client such as
claude.ai, a scheduled routine or a code sandbox has no way to put a file there,
so the server offers two more ways in and one more way out.

## Uploading

| Tool | How the bytes travel | Use for |
|------|----------------------|---------|
| `remarkable_upload_base64` | inline in the tool call, base64-encoded | small files (below ~30 KB): every character passes through the model |
| `remarkable_upload_link` | `curl -T file.pdf <url>` from the client's sandbox to a one-time HTTPS link | everything else; the file never enters the conversation |

Both take the same options as `remarkable_upload` plus:

- `tags`: document tags set on upload (cloud, SSH)
- `replace`: replace a document with the same name in the same folder (cloud).
  A document that already carries handwriting is **not** replaced unless
  `force_replace=True`; the replaced version goes to the trash.
- `create_folders`: create a missing `parent_folder` chain (cloud)

The server checks the bytes (PDF magic `%PDF-`, EPUB zip header) and the size
limit `REMARKABLE_MAX_UPLOAD_MB` (default 50) before anything reaches the tablet.

### One-time upload links

1. The client calls `remarkable_upload_link(document_name, parent_folder, ...)`.
   The options are bound to a random 256-bit token valid for 15 minutes.
2. The client sends the raw file: `curl -sS --fail-with-body -T plan.pdf "<upload_url>"`
   (PUT; POST also works).
3. The response body is the normal upload result (uuid, path, replaced versions).
   HTTP 200 = uploaded, 400 = rejected with a JSON reason, 404 = unknown/used/expired
   link, 413 = too large.

A link is consumed once an upload reaches the tablet. If the server rejects the
bytes as not being a PDF/EPUB, the same link can be retried.

## Downloading

`remarkable_export` returns a `download_url` next to the MCP resource link. It
serves the same temporary file (15 minutes, at most eight retained exports) at
`GET /files/export/<export_id>/<filename>`. To download everything with a tag,
call `remarkable_browse(tags=["..."])` and export each result.

## Configuration

Both HTTP routes exist only when the server runs with `--http` **and**
`REMARKABLE_PUBLIC_URL` is set to the externally reachable base URL, for example
`REMARKABLE_PUBLIC_URL=https://remarkable.example.org`. Without it,
`remarkable_upload_link` is not registered and exports carry no `download_url`.
The upload route also respects `--read-only` (it is not registered).

The routes are plain Starlette routes outside MCP authorization: the token or
export id in the path is the credential. If an authenticating reverse proxy sits
in front of the server, let `/files/` through without its login, for example
with Caddy:

```caddy
remarkable.example.org {
    handle /files/* {
        reverse_proxy 127.0.0.1:8000
    }
    handle {
        # existing authentication for the MCP endpoint
        reverse_proxy 127.0.0.1:8000
    }
}
```

Keep request-body limits at the proxy at least as large as `REMARKABLE_MAX_UPLOAD_MB`.

The client's sandbox must be allowed to reach that domain (for claude.ai, add it
to the code-execution network allowlist; for cloud routines, to the
environment's network policy).

## Tags

reMarkable stores tags in a document's `.content` file (`tags` for documents,
`pageTags` for pages), not in `.metadata`. The cloud and local-directory
transports read them from there; `remarkable_browse(tags=[...])` matches
document **and** page tags across the whole library (or below `path`). SSH mode
still reads tags only from `.metadata` and therefore usually sees none.
`remarkable_tag(document, add, remove)` changes document tags (cloud mode).
