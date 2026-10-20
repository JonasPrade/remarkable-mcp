"""Tests for tags, byte uploads (base64 / one-time links) and export download links."""

import base64
import json
import os
from unittest.mock import Mock, patch

import pytest

from remarkable_mcp import tags as tag_helpers
from remarkable_mcp.server import mcp
from remarkable_mcp.sync import ROOT_URL, RemarkableClient, _serialize_root_index

MINI_PDF = (
    b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\n"
    b"trailer<</Root 1 0 R>>\n%%EOF\n"
)


async def _call_tool(name: str, arguments: dict) -> dict:
    result = await mcp.call_tool(name, arguments)
    return json.loads(result.content[0].text)


# =============================================================================
# In-memory cloud
# =============================================================================


class FakeCloud(RemarkableClient):
    """RemarkableClient backed by an in-memory content-addressed blob store."""

    def __init__(self):
        super().__init__("device", "user")
        self.blobs = {}
        self.generation = 1
        body = _serialize_root_index([])
        self.root_hash = "0" * 64
        self.blobs[self.root_hash] = body

    def _get_file(self, file_hash, file_name):
        return self.blobs[file_hash]

    def _put_blob(self, content, blob_hash, filename, content_type="application/octet-stream"):
        self.blobs[blob_hash] = content

    def get_root(self):
        return self.root_hash, self.generation

    def _commit_root(self, root_hash, generation, broadcast=True):
        assert generation == self.generation
        self.root_hash = root_hash
        self.generation += 1
        return self.generation

    def _request(self, url, method="GET", **kwargs):
        assert url == ROOT_URL, url
        payload = {"hash": self.root_hash, "generation": self.generation}
        response = Mock()
        response.text = json.dumps(payload)
        response.json.return_value = payload
        response.raise_for_status.return_value = None
        return response

    def add_document(self, name, parent="", tags=(), page_tags=(), ink=False, folder=False):
        """Seed a document (optionally with ink) the way the tablet stores it."""
        import uuid

        doc_id = str(uuid.uuid4())
        metadata = {
            "visibleName": name,
            "type": "CollectionType" if folder else "DocumentType",
            "parent": parent,
            "lastModified": "1700000000000",
        }
        files = [
            self._upload_file_blob(json.dumps(metadata).encode(), f"{doc_id}.metadata"),
        ]
        if not folder:
            content = {
                "fileType": "notebook",
                "tags": [{"name": t, "timestamp": 1} for t in tags],
                "pageTags": [{"name": t, "pageId": "p1", "timestamp": 1} for t in page_tags],
            }
            files.append(self._upload_file_blob(json.dumps(content).encode(), f"{doc_id}.content"))
            if ink:
                files.append(self._upload_file_blob(b"reMarkable .lines file", f"{doc_id}/p1.rm"))
        entry = self._upload_doc_index(doc_id, files)
        self._sync_root(lambda entries: entries + [entry])
        return doc_id

    def content_of(self, doc_id):
        doc = self.get_doc(doc_id)
        entry = next(f for f in doc.files if f["id"].endswith(".content"))
        return json.loads(self.blobs[entry["hash"]])

    def live_docs(self):
        return [d for d in self.get_meta_items() if d.parent != "trash"]


@pytest.fixture
def cloud():
    fake = FakeCloud()
    with (
        patch("remarkable_mcp.write_tools.get_rmapi", return_value=fake),
        patch("remarkable_mcp.tools.get_rmapi", return_value=fake),
        patch("remarkable_mcp.tools._is_cloud_archived", return_value=False),
    ):
        yield fake


# =============================================================================
# tags.py
# =============================================================================


class TestTagHelpers:
    def test_tag_names_accepts_objects_and_strings(self):
        raw = [{"name": "work", "timestamp": 1}, "home", {"name": "work"}, {"x": 1}, 3]
        assert tag_helpers.tag_names(raw) == ["work", "home"]
        assert tag_helpers.tag_names(None) == []

    def test_apply_tag_changes_keeps_timestamps_and_is_case_insensitive(self):
        content = {"tags": [{"name": "Work", "timestamp": 5}, {"name": "old", "timestamp": 6}]}
        names = tag_helpers.apply_tag_changes(
            content, add=["work", "neu", " neu "], remove=["OLD"], timestamp_ms=9
        )
        assert names == ["Work", "neu"]
        assert content["tags"] == [
            {"name": "Work", "timestamp": 5},
            {"name": "neu", "timestamp": 9},
        ]

    def test_matches_tags_uses_page_tags(self):
        assert tag_helpers.matches_tags(["todo"], ["work"], ["TODO"])
        assert not tag_helpers.matches_tags(["todo"], ["work"], [])
        assert tag_helpers.matches_tags([], [], [])


# =============================================================================
# Cloud client: tags and annotations
# =============================================================================


class TestCloudTags:
    def test_load_reads_tags_from_content(self):
        cloud = FakeCloud()
        doc_id = cloud.add_document("Plan", tags=["work"], page_tags=["todo"])
        doc = cloud.get_doc(doc_id)
        assert doc.tags == ["work"]
        assert doc.page_tags == ["todo"]

    def test_upload_document_writes_tag_entries(self):
        cloud = FakeCloud()
        doc = cloud.upload_document(MINI_PDF, "Doc", "pdf", tags=["a", "A", "b"])
        assert doc.tags == ["a", "b"]
        loaded = cloud.get_doc(doc.id)
        assert loaded.tags == ["a", "b"]
        assert [t["name"] for t in cloud.content_of(doc.id)["tags"]] == ["a", "b"]

    def test_set_tags_updates_content_and_bumps_metadata(self):
        cloud = FakeCloud()
        doc_id = cloud.add_document("Plan", tags=["alt"])
        assert cloud.set_tags(doc_id, add=["neu"], remove=["alt"]) == ["neu"]
        doc = cloud.get_doc(doc_id)
        assert doc.tags == ["neu"]
        meta_entry = next(f for f in doc.files if f["id"].endswith(".metadata"))
        assert json.loads(cloud.blobs[meta_entry["hash"]])["metadatamodified"] is True

    def test_has_annotations(self):
        cloud = FakeCloud()
        plain = cloud.get_doc(cloud.add_document("Leer"))
        inked = cloud.get_doc(cloud.add_document("Beschrieben", ink=True))
        assert not cloud.has_annotations(plain)
        assert cloud.has_annotations(inked)


# =============================================================================
# Uploads
# =============================================================================


class TestUploadBase64:
    @pytest.mark.asyncio
    async def test_uploads_with_tags_into_new_folders(self, cloud):
        data = await _call_tool(
            "remarkable_upload_base64",
            {
                "content_base64": base64.b64encode(MINI_PDF).decode(),
                "document_name": "Tagesplan 2026-10-19",
                "parent_folder": "/Tagesplan/Test",
                "tags": ["tagesplan"],
                "create_folders": True,
            },
        )
        assert data["uploaded"] is True, data
        assert data["path"] == "/Tagesplan/Test/Tagesplan 2026-10-19"
        assert data["tags"] == ["tagesplan"]
        docs = {d.name: d for d in cloud.live_docs()}
        assert docs["Tagesplan"].is_folder and docs["Test"].parent == docs["Tagesplan"].id
        assert docs["Tagesplan 2026-10-19"].parent == docs["Test"].id
        assert docs["Tagesplan 2026-10-19"].tags == ["tagesplan"]

    @pytest.mark.asyncio
    async def test_missing_folder_without_create_folders(self, cloud):
        data = await _call_tool(
            "remarkable_upload_base64",
            {
                "content_base64": base64.b64encode(MINI_PDF).decode(),
                "document_name": "X",
                "parent_folder": "/Fehlt",
            },
        )
        assert data["_error"]["type"] == "folder_not_found"

    @pytest.mark.asyncio
    async def test_rejects_invalid_base64_and_non_pdf(self, cloud):
        bad = await _call_tool(
            "remarkable_upload_base64", {"content_base64": "%%%", "document_name": "X"}
        )
        assert bad["_error"]["type"] == "invalid_base64"
        not_pdf = await _call_tool(
            "remarkable_upload_base64",
            {"content_base64": base64.b64encode(b"hello").decode(), "document_name": "X"},
        )
        assert not_pdf["_error"]["type"] == "invalid_file"
        assert cloud.live_docs() == []

    @pytest.mark.asyncio
    async def test_accepts_data_uri_and_line_breaks(self, cloud):
        encoded = base64.b64encode(MINI_PDF).decode()
        wrapped = "data:application/pdf;base64," + "\n".join(
            encoded[i : i + 60] for i in range(0, len(encoded), 60)
        )
        data = await _call_tool(
            "remarkable_upload_base64", {"content_base64": wrapped, "document_name": "X"}
        )
        assert data["uploaded"] is True, data

    @pytest.mark.asyncio
    async def test_size_limit(self, cloud):
        with patch.dict(os.environ, {"REMARKABLE_MAX_UPLOAD_MB": "0.1"}):
            data = await _call_tool(
                "remarkable_upload_base64",
                {
                    "content_base64": base64.b64encode(MINI_PDF + b" " * 200_000).decode(),
                    "document_name": "X",
                },
            )
        assert data["_error"]["type"] == "upload_too_large"


class TestReplace:
    async def _upload(self, **extra):
        args = {
            "content_base64": base64.b64encode(MINI_PDF).decode(),
            "document_name": "Plan",
            "parent_folder": "/",
        }
        args.update(extra)
        return await _call_tool("remarkable_upload_base64", args)

    @pytest.mark.asyncio
    async def test_without_replace_keeps_both_and_says_so(self, cloud):
        cloud.add_document("Plan")
        data = await self._upload()
        assert "already existed" in data["note"]
        assert [d.name for d in cloud.live_docs()].count("Plan") == 2

    @pytest.mark.asyncio
    async def test_replace_trashes_unannotated_document(self, cloud):
        old_id = cloud.add_document("Plan")
        data = await self._upload(replace=True)
        assert data["replaced"] == [{"uuid": old_id, "had_annotations": False}]
        live = cloud.live_docs()
        assert [d.name for d in live] == ["Plan"] and live[0].id == data["uuid"]
        assert cloud.get_doc(old_id).parent == "trash"

    @pytest.mark.asyncio
    async def test_replace_refuses_document_with_ink(self, cloud):
        old_id = cloud.add_document("Plan", ink=True)
        data = await self._upload(replace=True)
        assert data["_error"]["type"] == "has_annotations"
        assert [d.id for d in cloud.live_docs()] == [old_id]

    @pytest.mark.asyncio
    async def test_force_replace_replaces_document_with_ink(self, cloud):
        old_id = cloud.add_document("Plan", ink=True)
        data = await self._upload(replace=True, force_replace=True)
        assert data["replaced"] == [{"uuid": old_id, "had_annotations": True}]
        assert cloud.get_doc(old_id).parent == "trash"

    @pytest.mark.asyncio
    async def test_replace_only_matches_same_folder(self, cloud):
        folder = cloud.add_document("Archiv", folder=True)
        other = cloud.add_document("Plan", parent=folder)
        data = await self._upload(replace=True)
        assert "replaced" not in data
        assert cloud.get_doc(other).parent == folder


# =============================================================================
# Tag tool and browse filter
# =============================================================================


class TestTagTool:
    @pytest.mark.asyncio
    async def test_add_and_remove(self, cloud):
        cloud.add_document("Plan", tags=["alt"])
        data = await _call_tool("remarkable_tag", {"document": "Plan", "add": ["neu"]})
        assert data["tags"] == ["alt", "neu"]
        data = await _call_tool("remarkable_tag", {"document": "Plan", "remove": ["ALT"]})
        assert data["tags"] == ["neu"]

    @pytest.mark.asyncio
    async def test_errors(self, cloud):
        cloud.add_document("Ordner", folder=True)
        assert (await _call_tool("remarkable_tag", {"document": "Plan"}))["_error"][
            "type"
        ] == "nothing_to_do"
        data = await _call_tool("remarkable_tag", {"document": "Ordner", "add": ["x"]})
        assert data["_error"]["type"] == "document_not_found"


class TestBrowseTags:
    @pytest.mark.asyncio
    async def test_filter_searches_subtrees_and_page_tags(self, cloud):
        work = cloud.add_document("Arbeit", folder=True)
        cloud.add_document("Doc tag", parent=work, tags=["review"])
        cloud.add_document("Page tag", page_tags=["review"])
        cloud.add_document("Untagged", parent=work)

        data = await _call_tool("remarkable_browse", {"tags": ["Review"]})
        assert data["mode"] == "search"
        assert sorted(r["name"] for r in data["results"]) == ["Doc tag", "Page tag"]
        page = next(r for r in data["results"] if r["name"] == "Page tag")
        assert page["page_tags"] == ["review"]

        scoped = await _call_tool("remarkable_browse", {"path": "/Arbeit", "tags": ["review"]})
        assert [r["name"] for r in scoped["results"]] == ["Doc tag"]

    @pytest.mark.asyncio
    async def test_folder_listing_shows_tags(self, cloud):
        cloud.add_document("Plan", tags=["work"], page_tags=["todo"])
        data = await _call_tool("remarkable_browse", {"path": "/"})
        assert data["documents"][0]["tags"] == ["work"]
        assert data["documents"][0]["page_tags"] == ["todo"]


# =============================================================================
# One-time links
# =============================================================================


def _request(path_params, body=b"", method="PUT", headers=None):
    from starlette.requests import Request

    chunks = [body]

    async def receive():
        chunk = chunks.pop(0) if chunks else b""
        return {"type": "http.request", "body": chunk, "more_body": False}

    raw_headers = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope = {
        "type": "http",
        "method": method,
        "path": "/",
        "headers": raw_headers,
        "path_params": path_params,
        "query_string": b"",
    }
    return Request(scope, receive)


class TestUploadLinkStore:
    def test_single_use_and_retry_after_rejection(self):
        from remarkable_mcp.file_links import UploadLinkStore, UploadRequest

        store = UploadLinkStore()
        token, _ = store.create(UploadRequest(document_name="X"))
        assert store.claim(token) is not None
        assert store.claim(token) is None  # busy while in use
        store.release(token, consumed=False)
        assert store.claim(token) is not None
        store.release(token, consumed=True)
        assert store.claim(token) is None

    def test_expiry_and_bound(self):
        from remarkable_mcp.file_links import UploadLinkStore, UploadRequest

        now = [0.0]
        store = UploadLinkStore(ttl_seconds=10, max_pending=2, monotonic_clock=lambda: now[0])
        first, _ = store.create(UploadRequest(document_name="1"))
        store.create(UploadRequest(document_name="2"))
        store.create(UploadRequest(document_name="3"))
        assert store.claim(first) is None  # evicted by the bound
        late, _ = store.create(UploadRequest(document_name="4"))
        now[0] = 11
        assert store.claim(late) is None


class TestUploadLinkRoute:
    @pytest.mark.asyncio
    async def test_put_uploads_and_consumes_token(self, cloud):
        from remarkable_mcp.file_links import UploadRequest, handle_upload, upload_links

        token, _ = upload_links.create(
            UploadRequest(document_name="Per Link", tags=["tagesplan"], create_folders=True)
        )
        response = await handle_upload(_request({"token": token}, MINI_PDF))
        assert response.status_code == 200, response.body
        assert json.loads(response.body)["uploaded"] is True
        assert cloud.live_docs()[0].tags == ["tagesplan"]

        again = await handle_upload(_request({"token": token}, MINI_PDF))
        assert again.status_code == 404

    @pytest.mark.asyncio
    async def test_bad_bytes_keep_token_for_retry(self, cloud):
        from remarkable_mcp.file_links import UploadRequest, handle_upload, upload_links

        token, _ = upload_links.create(UploadRequest(document_name="Retry"))
        bad = await handle_upload(_request({"token": token}, b"<html>"))
        assert bad.status_code == 400
        assert json.loads(bad.body)["_error"]["type"] == "invalid_file"
        good = await handle_upload(_request({"token": token}, MINI_PDF))
        assert good.status_code == 200

    @pytest.mark.asyncio
    async def test_declared_oversize_is_rejected(self, cloud):
        from remarkable_mcp.file_links import UploadRequest, handle_upload, upload_links

        token, _ = upload_links.create(UploadRequest(document_name="Big"))
        with patch.dict(os.environ, {"REMARKABLE_MAX_UPLOAD_MB": "0.1"}):
            response = await handle_upload(
                _request({"token": token}, MINI_PDF, headers={"Content-Length": "999999"})
            )
        assert response.status_code == 413

    @pytest.mark.asyncio
    async def test_unknown_token(self):
        from remarkable_mcp.file_links import handle_upload

        response = await handle_upload(_request({"token": "nope"}, MINI_PDF))
        assert response.status_code == 404

    def test_link_tool_only_with_public_url(self):
        from remarkable_mcp.file_links import public_base_url

        with patch.dict(os.environ, {"REMARKABLE_PUBLIC_URL": ""}):
            assert public_base_url() is None
        with patch.dict(os.environ, {"REMARKABLE_PUBLIC_URL": "https://rm.example/"}):
            assert public_base_url() == "https://rm.example"
        with patch.dict(os.environ, {"REMARKABLE_PUBLIC_URL": "rm.example"}):
            assert public_base_url() is None

    @pytest.mark.asyncio
    async def test_link_tool_returns_url(self):
        from remarkable_mcp.file_links import register_upload_link_tool, upload_links

        with patch.dict(os.environ, {"REMARKABLE_PUBLIC_URL": "https://rm.example"}):
            register_upload_link_tool()
            try:
                data = await _call_tool(
                    "remarkable_upload_link",
                    {"document_name": "Plan", "parent_folder": "/Tagesplan"},
                )
            finally:
                mcp._tool_manager._tools.pop("remarkable_upload_link", None)
                mcp._custom_starlette_routes[:] = [
                    r for r in mcp._custom_starlette_routes if r.path != "/files/upload/{token}"
                ]
        assert data["upload_url"].startswith("https://rm.example/files/upload/")
        token = data["upload_url"].rsplit("/", 1)[1]
        assert upload_links.claim(token).parent_folder == "/Tagesplan"
        assert "curl" in data["curl"]


class TestExportDownload:
    @pytest.mark.asyncio
    async def test_download_route_serves_export(self):
        from remarkable_mcp.export_resources import export_store
        from remarkable_mcp.file_links import export_download_url, handle_export_download

        published = export_store.publish(
            filename="Plan", output_format="pdf", writer=lambda p: p.write_bytes(MINI_PDF)
        )
        export_id = published.resource.export_id
        response = await handle_export_download(
            _request({"export_id": export_id, "filename": "Plan.pdf"}, method="GET")
        )
        assert response.status_code == 200
        assert response.body == MINI_PDF
        assert response.media_type == "application/pdf"

        missing = await handle_export_download(
            _request({"export_id": "0" * 32, "filename": "x"}, method="GET")
        )
        assert missing.status_code == 404

        with patch.dict(os.environ, {"REMARKABLE_PUBLIC_URL": "https://rm.example"}):
            url = export_download_url(export_id, "Tages plan.pdf")
        assert url == f"https://rm.example/files/export/{export_id}/Tages%20plan.pdf"
        with patch.dict(os.environ, {"REMARKABLE_PUBLIC_URL": ""}):
            assert export_download_url(export_id, "x.pdf") is None
