#!/usr/bin/env python3
"""Tests for the local file receipt storage backend (server-side).

Covers:
  * LocalFileBackend: list / read / remove, ResourceNotFoundError,
    path-traversal protection, missing-folder tolerance.
  * Configuration.load(): backend selection via the ``documents.backend``
    key, and fallback inference for legacy configs.
"""

import json
import os
import pathlib
import sys
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent.parent))

from beancount_ai.server.config import (
    Configuration,
    LocalFileDocumentSourcesConfiguration,
    WebDAVDocumentSourcesConfiguration,
)
from beancount_ai.server.storage import LocalFileBackend, ResourceNotFoundError

# ===========================================================================
# Helpers
# ===========================================================================


def _local_config(tmp_path: Path) -> LocalFileDocumentSourcesConfiguration:
    cfg = LocalFileDocumentSourcesConfiguration()
    cfg.uningested_receipts_folder = tmp_path / "receipts" / "uningested"
    cfg.unassociated_receipts_folder = tmp_path / "receipts" / "unassociated"
    return cfg


def _seed(base: Path, name: str, content: bytes) -> Path:
    base.mkdir(parents=True, exist_ok=True)
    p = base / name
    p.write_bytes(content)
    return p


def _write_server_config(
    tmp_path: Path, documents: Mapping[str, object], name: str = "cfg.json"
) -> Path:
    data = {
        "ai": {
            "api_url": "http://llm.example.com/v1",
            "token": "tok",
            "model_name": "model",
        },
        "documents": documents,
    }
    fp = tmp_path / name
    fp.write_text(json.dumps(data))
    return fp


WEBDAV_DOCS = {
    "backend": "webdav",
    "username": "u",
    "password": "p",
    "base_url": "https://dav.example.com/files",
    "uningested_receipts_subfolder": "un",
    "unassociated_receipts_subfolder": "ua",
}


LOCAL_DOC_FIELDS = {
    "uningested_receipts_folder": "/data/uningested",
    "unassociated_receipts_folder": "/data/unassociated",
}


@pytest.fixture
def server_config_instance(tmp_path: Path) -> Iterator[Configuration]:
    """Yield a loaded server-side Configuration instance for a local backend."""
    fp = _write_server_config(
        tmp_path,
        {
            "backend": "local",
            "uningested_receipts_folder": str(tmp_path / "rcpts" / "uningested"),
            "unassociated_receipts_folder": str(tmp_path / "rcpts" / "unassociated"),
        },
    )
    with mock.patch.object(Configuration, "instance", None):
        yield Configuration.load(str(fp))


# ===========================================================================
# LocalFileBackend
# ===========================================================================


class TestList:
    def test_empty_when_folder_missing(self, tmp_path: Path) -> None:
        cfg = _local_config(tmp_path)
        backend = LocalFileBackend(cfg, "uningested")
        assert backend.list() == []

    def test_lists_files_with_metadata(self, tmp_path: Path) -> None:
        cfg = _local_config(tmp_path)
        folder = cfg.receipts_uningested_folder()
        _seed(folder, "receipt.jpg", b"imgdata")
        (folder / "subdir").mkdir()  # directory entries are skipped
        (folder / "subdir" / "nested.jpg").write_bytes(b"n")

        backend = LocalFileBackend(cfg, "uningested")
        items = backend.list()
        assert [i["name"] for i in items] == ["receipt.jpg"]
        assert items[0]["content_length"] == len(b"imgdata")
        assert isinstance(items[0]["modified"], datetime)
        assert items[0]["modified"].tzinfo is not None

    def test_category_selects_folder(self, tmp_path: Path) -> None:
        cfg = _local_config(tmp_path)
        _seed(cfg.receipts_unassociated_folder(), "a.pdf", b"x")
        assert LocalFileBackend(cfg, "uningested").list() == []
        names = [i["name"] for i in LocalFileBackend(cfg, "unassociated").list()]
        assert names == ["a.pdf"]


class TestRead:
    def test_reads_bytes(self, tmp_path: Path) -> None:
        cfg = _local_config(tmp_path)
        _seed(cfg.receipts_uningested_folder(), "r.png", b"pixels")
        backend = LocalFileBackend(cfg, "uningested")
        assert backend.read("r.png") == b"pixels"
        assert backend.read("/r.png") == b"pixels"

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        cfg = _local_config(tmp_path)
        cfg.receipts_uningested_folder().mkdir(parents=True, exist_ok=True)
        backend = LocalFileBackend(cfg, "uningested")
        with pytest.raises(ResourceNotFoundError):
            backend.read("nope.jpg")

    def test_traversal_is_sandboxed(self, tmp_path: Path) -> None:
        cfg = _local_config(tmp_path)
        outside = _seed(tmp_path, "outside.jpg", b"secret")
        backend = LocalFileBackend(cfg, "uningested")
        with pytest.raises(ResourceNotFoundError):
            backend.read("../outside.jpg")
        assert outside.exists()


class TestRemove:
    def test_removes_existing_file(self, tmp_path: Path) -> None:
        cfg = _local_config(tmp_path)
        p = _seed(cfg.receipts_uningested_folder(), "r.jpg", b"z")
        LocalFileBackend(cfg, "uningested").remove("r.jpg")
        assert not p.exists()

    def test_removing_missing_file_raises(self, tmp_path: Path) -> None:
        cfg = _local_config(tmp_path)
        cfg.receipts_uningested_folder().mkdir(parents=True, exist_ok=True)
        with pytest.raises(ResourceNotFoundError):
            LocalFileBackend(cfg, "uningested").remove("nope.jpg")

    def test_resource_not_found_is_file_not_found(self) -> None:
        assert issubclass(ResourceNotFoundError, FileNotFoundError)


# ===========================================================================
# Configuration loading
# ===========================================================================


class TestLoadDocuments:
    def test_explicit_local_backend(self, tmp_path: Path) -> None:
        fp = _write_server_config(
            tmp_path,
            {"backend": "local", **LOCAL_DOC_FIELDS},
            name="local.json",
        )
        with mock.patch.object(Configuration, "instance", None):
            cfg = Configuration.load(str(fp))
        assert isinstance(cfg.documents, LocalFileDocumentSourcesConfiguration)
        assert cfg.documents.uningested_receipts_folder == Path("/data/uningested")
        assert (
            cfg.documents.unassociated_receipts_folder == Path("/data/unassociated")
        )
        assert (
            cfg.documents.receipts_uningested_folder() == Path("/data/uningested")
        )
        assert (
            cfg.documents.receipts_unassociated_folder()
            == Path("/data/unassociated")
        )

    def test_independent_folders(self, tmp_path: Path) -> None:
        """The two receipt folders may live in unrelated directories."""
        fp = _write_server_config(
            tmp_path,
            {
                "backend": "local",
                "uningested_receipts_folder": "/a/b/c",
                "unassociated_receipts_folder": "/completely/other/place",
            },
            name="custom.json",
        )
        with mock.patch.object(Configuration, "instance", None):
            cfg = Configuration.load(str(fp))
        assert isinstance(cfg.documents, LocalFileDocumentSourcesConfiguration)
        assert cfg.documents.uningested_receipts_folder == Path("/a/b/c")
        assert (
            cfg.documents.unassociated_receipts_folder
            == Path("/completely/other/place")
        )
        assert cfg.documents.receipts_uningested_folder() == Path("/a/b/c")
        assert (
            cfg.documents.receipts_unassociated_folder()
            == Path("/completely/other/place")
        )

    def test_implicit_local_backend(self, tmp_path: Path) -> None:
        fp = _write_server_config(tmp_path, dict(LOCAL_DOC_FIELDS))
        with mock.patch.object(Configuration, "instance", None):
            cfg = Configuration.load(str(fp))
        assert isinstance(cfg.documents, LocalFileDocumentSourcesConfiguration)
        assert (
            cfg.documents.uningested_receipts_folder == Path("/data/uningested")
        )

    def test_legacy_webdav_config_still_works(self, tmp_path: Path) -> None:
        legacy = {k: v for k, v in WEBDAV_DOCS.items() if k != "backend"}
        fp = _write_server_config(tmp_path, legacy, name="legacy.json")
        with mock.patch.object(Configuration, "instance", None):
            cfg = Configuration.load(str(fp))
        assert isinstance(cfg.documents, WebDAVDocumentSourcesConfiguration)
        assert cfg.documents.username == "u"
        assert cfg.documents.base_url == "https://dav.example.com/files"

    def test_explicit_webdav_backend(self, tmp_path: Path) -> None:
        fp = _write_server_config(tmp_path, dict(WEBDAV_DOCS), name="webdav.json")
        with mock.patch.object(Configuration, "instance", None):
            cfg = Configuration.load(str(fp))
        assert isinstance(cfg.documents, WebDAVDocumentSourcesConfiguration)

    def test_implicit_webdav_when_both_local_keys_absent(
        self, tmp_path: Path
    ) -> None:
        fp = _write_server_config(tmp_path, dict(WEBDAV_DOCS), name="imp.json")
        with mock.patch.object(Configuration, "instance", None):
            cfg = Configuration.load(str(fp))
        assert isinstance(cfg.documents, WebDAVDocumentSourcesConfiguration)

    def test_explicit_backend_wins_over_inferred(
        self, tmp_path: Path
    ) -> None:
        # Explicit backend=local is honored even when a webdav key is also
        # present.
        fp = _write_server_config(
            tmp_path,
            {"backend": "local", **LOCAL_DOC_FIELDS, "base_url": "https://x"},
            name="mixed.json",
        )
        with mock.patch.object(Configuration, "instance", None):
            cfg = Configuration.load(str(fp))
        assert isinstance(cfg.documents, LocalFileDocumentSourcesConfiguration)

    def test_local_backend_missing_key_raises(self, tmp_path: Path) -> None:
        fp = _write_server_config(
            tmp_path,
            {"backend": "local", "uningested_receipts_folder": "/data/uningested"},
            name="local_missing.json",
        )
        with (
            mock.patch.object(Configuration, "instance", None),
            pytest.raises(ValueError, match="unassociated_receipts_folder"),
        ):
            Configuration.load(str(fp))

    def test_webdav_backend_missing_key_raises(self, tmp_path: Path) -> None:
        fp = _write_server_config(
            tmp_path,
            {
                "backend": "webdav",
                "uningested_receipts_subfolder": "un",
                "unassociated_receipts_subfolder": "ua",
            },
            name="webdav_missing.json",
        )
        with (
            mock.patch.object(Configuration, "instance", None),
            pytest.raises(ValueError, match="base_url"),
        ):
            Configuration.load(str(fp))

    def test_unknown_backend_raises(self, tmp_path: Path) -> None:
        fp = _write_server_config(
            tmp_path, {"backend": "s3", **LOCAL_DOC_FIELDS}
        )
        with (
            mock.patch.object(Configuration, "instance", None),
            pytest.raises(ValueError, match="unknown documents backend"),
        ):
            Configuration.load(str(fp))


class TestLocationNames:
    def test_local_names(self, tmp_path: Path) -> None:
        cfg = _local_config(tmp_path)
        assert str(cfg.receipts_uningested_folder()) in cfg.uningested_location_name()
        assert str(cfg.receipts_unassociated_folder()) in cfg.unassociated_location_name()

    def test_webdav_names(self) -> None:
        cfg = WebDAVDocumentSourcesConfiguration()
        cfg.username = "u"
        cfg.password = "p"
        cfg.base_url = "https://dav.example.com"
        cfg.uningested_receipts_subfolder = "un"
        cfg.unassociated_receipts_subfolder = "ua"
        assert "https://dav.example.com/un" in cfg.uningested_location_name()
        assert "https://dav.example.com/ua" in cfg.unassociated_location_name()


class TestBackendIntegration:
    def test_loaded_config_produces_working_backend(
        self, server_config_instance: Configuration
    ) -> None:
        docs = server_config_instance.documents
        assert isinstance(docs, LocalFileDocumentSourcesConfiguration)
        _seed(docs.uningested_receipts_folder, "a.jpg", b"A")
        _seed(docs.unassociated_receipts_folder, "b.pdf", b"B")

        uningested = LocalFileBackend(docs, "uningested")
        unassociated = LocalFileBackend(docs, "unassociated")

        assert [i["name"] for i in uningested.list()] == ["a.jpg"]
        assert [i["name"] for i in unassociated.list()] == ["b.pdf"]
        assert uningested.read("a.jpg") == b"A"
        uningested.remove("a.jpg")
        assert [i["name"] for i in uningested.list()] == []
        assert unassociated.read("b.pdf") == b"B"


class TestModifiedIsSortable:
    def test_modified_timestamps_are_comparable(self, tmp_path: Path) -> None:
        cfg = _local_config(tmp_path)
        folder = cfg.receipts_uningested_folder()
        a = _seed(folder, "a.jpg", b"A")
        b = _seed(folder, "b.jpg", b"B")
        # Force distinct, known mtimes so the sort order is deterministic.
        t1 = datetime(2026, 1, 1, tzinfo=UTC).timestamp()
        t2 = datetime(2026, 2, 1, tzinfo=UTC).timestamp()
        os.utime(a, (t1, t1))
        os.utime(b, (t2, t2))
        items = LocalFileBackend(cfg, "uningested").list()
        assert [i["name"] for i in sorted(items, key=lambda i: i["modified"])] == [
            "a.jpg",
            "b.jpg",
        ]
