#!/usr/bin/env python3
"""Tests for the client-side receipt reference (local vs. store) resolution.

Covers the D1 selection matrix in ``docs/specs/Client-side receipts.md``:
path-like vs. bare-name classification, the ``VALID_EXTENSIONS`` gate on bare
names, the fail-fast errors for broken/missing paths, the
bare-name collision notice, and ``load()`` for both sources.
"""

import os
import pathlib
import sys
from unittest import mock

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent.parent))

from beanhand.client.receipts import (
    ReceiptRef,
    Source,
    maybe_warn_collision,
)
from beanhand.client.server import documents as documents_client
from beanhand.structs import FetchedReceipt

SAMPLE_BYTES = b"%PDF-1.4 fake receipt body"
SAMPLE_TS = 1_700_000_000.5


# ===========================================================================
# resolve(): D1 selection matrix
# ===========================================================================


class TestResolvePathLike:
    def test_absolute_existing_is_local(self, tmp_path: pathlib.Path) -> None:
        f = tmp_path / "scan" / "x.pdf"
        f.parent.mkdir()
        f.write_bytes(SAMPLE_BYTES)

        ref = ReceiptRef.resolve([str(f)])[0]

        assert ref.src is Source.LOCAL
        assert ref.path == f
        assert ref.filename == "x.pdf"
        assert ref.arg == str(f)

    def test_relative_existing_is_local(
        self, tmp_path: pathlib.Path, monkeypatch: "pytest.MonkeyPatch"
    ) -> None:
        sub = tmp_path / "sub"
        sub.mkdir()
        f = sub / "x.jpg"
        f.write_bytes(SAMPLE_BYTES)
        monkeypatch.chdir(tmp_path)

        ref = ReceiptRef.resolve(["sub/x.jpg"])[0]

        assert ref.src is Source.LOCAL
        assert ref.path.resolve() == f
        assert ref.filename == "x.jpg"

    def test_dot_slash_existing_is_local(
        self, tmp_path: pathlib.Path, monkeypatch: "pytest.MonkeyPatch"
    ) -> None:
        f = tmp_path / "x.png"
        f.write_bytes(SAMPLE_BYTES)
        monkeypatch.chdir(tmp_path)

        ref = ReceiptRef.resolve(["./x.png"])[0]

        assert ref.src is Source.LOCAL
        assert ref.path.name == "x.png"

    def test_missing_pathlike_is_error(self, tmp_path: pathlib.Path) -> None:
        with pytest.raises(FileNotFoundError, match="x.jpg"):
            ReceiptRef.resolve([str(tmp_path / "nope" / "x.jpg")])[0]

    def test_absent_relative_path_is_error(
        self, tmp_path: pathlib.Path, monkeypatch: "pytest.MonkeyPatch"
    ) -> None:
        monkeypatch.chdir(tmp_path)
        with pytest.raises(FileNotFoundError, match="x.jpg"):
            ReceiptRef.resolve(["scans/x.jpg"])[0]

    def test_directory_is_error(self, tmp_path: pathlib.Path) -> None:
        d = tmp_path / "subdir"
        d.mkdir()
        with pytest.raises(IsADirectoryError, match="is a directory"):
            ReceiptRef.resolve([str(d)])[0]


class TestResolveBareName:
    def test_existing_receipt_ext_is_local(
        self, tmp_path: pathlib.Path, monkeypatch: "pytest.MonkeyPatch"
    ) -> None:
        f = tmp_path / "r.pdf"
        f.write_bytes(SAMPLE_BYTES)
        monkeypatch.chdir(tmp_path)

        ref = ReceiptRef.resolve(["r.pdf"])[0]

        assert ref.src is Source.LOCAL
        assert ref.filename == "r.pdf"

    @pytest.mark.parametrize("name", ["a.jpg", "b.jpeg", "c.png", "d.pdf"])
    def test_each_receipt_ext_is_local(
        self, tmp_path: pathlib.Path, monkeypatch: "pytest.MonkeyPatch", name: str
    ) -> None:
        f = tmp_path / name
        f.write_bytes(SAMPLE_BYTES)
        monkeypatch.chdir(tmp_path)

        assert ReceiptRef.resolve([name])[0].src is Source.LOCAL

    def test_bare_non_receipt_ext_is_store(
        self, tmp_path: pathlib.Path, monkeypatch: "pytest.MonkeyPatch"
    ) -> None:
        # A bare name without a receipt extension is a store filename even
        # though a same-named file sits in the CWD (today's behavior).
        (tmp_path / "notes.txt").write_text("hello")
        monkeypatch.chdir(tmp_path)

        ref = ReceiptRef.resolve(["notes.txt"])[0]

        assert ref.src is Source.STORE
        assert ref.filename == "notes.txt"

    def test_bare_missing_is_store(
        self, tmp_path: pathlib.Path, monkeypatch: "pytest.MonkeyPatch"
    ) -> None:
        # Run from a directory with no same-named file so the CWD lookup finds
        # nothing and the name falls through to the store.
        monkeypatch.chdir(tmp_path)
        ref = ReceiptRef.resolve(["does-not-exist.jpg"])[0]
        assert ref.src is Source.STORE
        assert ref.filename == "does-not-exist.jpg"


class TestResolveSymlinks:
    def test_broken_symlink_is_error(self, tmp_path: pathlib.Path) -> None:
        link = tmp_path / "broken-link.jpg"
        link.symlink_to(tmp_path / "no-target.jpg")

        with pytest.raises(FileNotFoundError, match="broken symlink"):
            ReceiptRef.resolve([str(link)])[0]

    def test_relative_broken_symlink_is_error(
        self, tmp_path: pathlib.Path, monkeypatch: "pytest.MonkeyPatch"
    ) -> None:
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "broken.jpg").symlink_to("no-target.jpg")
        monkeypatch.chdir(tmp_path)

        # A pathlike broken symlink is an error, not a silent fall-through to
        # the store (a bare name would instead be a store filename).
        with pytest.raises(FileNotFoundError, match="broken symlink"):
            ReceiptRef.resolve(["sub/broken.jpg"])[0]

    def test_bare_broken_symlink_is_store(
        self, tmp_path: pathlib.Path, monkeypatch: "pytest.MonkeyPatch"
    ) -> None:
        link = tmp_path / "broken.jpg"
        link.symlink_to("no-target.jpg")
        monkeypatch.chdir(tmp_path)

        # A bare name that is not a usable file is a store filename (today's
        # behavior), even when it turns out to be a broken symlink in the CWD.
        assert ReceiptRef.resolve(["broken.jpg"])[0].src is Source.STORE


# ===========================================================================
# load(): local bytes + mtime, store fetch
# ===========================================================================


class TestLoad:
    def test_local_reads_bytes_and_mtime(self, tmp_path: pathlib.Path) -> None:
        f = tmp_path / "r.pdf"
        f.write_bytes(SAMPLE_BYTES)
        mtime = 1_650_000_000.25
        os.utime(f, (mtime, mtime))

        ref = ReceiptRef.resolve([str(f)])[0]
        fetched = ref.load(mock.MagicMock())

        assert fetched.data == SAMPLE_BYTES
        assert fetched.timestamp == mtime
        assert fetched.filename == "r.pdf"

    def test_store_branch_fetches(self, tmp_path: pathlib.Path) -> None:
        docs = mock.MagicMock()
        docs.fetch_receipt.return_value = FetchedReceipt(
            SAMPLE_BYTES, SAMPLE_TS, "r.pdf"
        )

        ref = ReceiptRef(Source.STORE, "r.pdf", pathlib.Path("r.pdf"), "r.pdf")
        fetched = ref.load(docs)

        docs.fetch_receipt.assert_called_once_with("r.pdf")
        assert fetched.data == SAMPLE_BYTES

    def test_local_race_raises_file_not_found(self, tmp_path: pathlib.Path) -> None:
        f = tmp_path / "gone.pdf"
        f.write_bytes(SAMPLE_BYTES)
        ref = ReceiptRef.resolve([str(f)])[0]
        f.unlink()  # vanishes between resolve and load

        with pytest.raises(FileNotFoundError):
            ref.load(mock.MagicMock())


# ===========================================================================
# resolve(list): a batch must not contain the same receipt twice (D7)
# ===========================================================================


class TestResolveMany:
    def test_same_store_name_collapses(self, tmp_path: pathlib.Path) -> None:
        out = ReceiptRef.resolve(["s.pdf", "s.pdf"])
        assert len(out) == 1
        assert out[0].src is Source.STORE
        assert out[0].filename == "s.pdf"

    def test_local_same_file_different_spellings_collapses(
        self, tmp_path: pathlib.Path, monkeypatch: "pytest.MonkeyPatch"
    ) -> None:
        f = tmp_path / "x.pdf"
        f.write_bytes(SAMPLE_BYTES)
        monkeypatch.chdir(tmp_path)
        # Same physical file, relative name and absolute path: collapses to one,
        # first-seen order preserved.
        out = ReceiptRef.resolve(["x.pdf", str(f)])
        assert len(out) == 1
        assert out[0].src is Source.LOCAL
        assert out[0].path.resolve() == f

    def test_different_locals_kept(
        self, tmp_path: pathlib.Path, monkeypatch: "pytest.MonkeyPatch"
    ) -> None:
        (tmp_path / "a.pdf").write_bytes(SAMPLE_BYTES)
        (tmp_path / "b.pdf").write_bytes(SAMPLE_BYTES)
        monkeypatch.chdir(tmp_path)
        out = ReceiptRef.resolve(["a.pdf", "b.pdf"])
        assert [r.filename for r in out] == ["a.pdf", "b.pdf"]

    def test_order_preserved_and_deduped(self, tmp_path: pathlib.Path) -> None:
        out = ReceiptRef.resolve(["first.pdf", "second.pdf", "first.pdf"])
        assert [r.filename for r in out] == ["first.pdf", "second.pdf"]

    def test_empty_list(self, tmp_path: pathlib.Path) -> None:
        assert ReceiptRef.resolve([]) == []

    def test_local_and_store_same_basename_not_collapsed(
        self, tmp_path: pathlib.Path, monkeypatch: "pytest.MonkeyPatch"
    ) -> None:
        # A local file named r.pdf and a store receipt named r.pdf are distinct
        # receipts (different universes): the Source-prefixed key keeps them
        # apart.  A bare CWD name always resolves local, so the store ref is
        # built directly to compare its dedup identity.
        (tmp_path / "r.pdf").write_bytes(SAMPLE_BYTES)
        monkeypatch.chdir(tmp_path)
        local = ReceiptRef.resolve(["r.pdf"])[0]
        assert local.src is Source.LOCAL
        store = ReceiptRef(Source.STORE, "r.pdf", pathlib.Path("r.pdf"), "r.pdf")
        assert local.dedup_key() != store.dedup_key()


# ===========================================================================
# maybe_warn_collision(): bare-name local/store collision (D1)
# ===========================================================================


class TestCollisionNotice:
    def _bare_local_ref(self) -> ReceiptRef:
        return ReceiptRef(Source.LOCAL, "r.pdf", pathlib.Path("r.pdf"), "r.pdf")

    def test_bare_local_present_in_store_prints_notice(
        self, tmp_path: pathlib.Path, capsys: "pytest.CaptureFixture[str]"
    ) -> None:
        docs = mock.MagicMock()
        docs.list_receipts.return_value = ["r.pdf"]
        with mock.patch("os.getcwd", return_value=str(tmp_path)):
            maybe_warn_collision(docs, "uningested", "r.pdf", self._bare_local_ref())

        err = capsys.readouterr().err
        assert "Using local file 'r.pdf'" in err
        assert "'r.pdf'" in err
        # The notice is the only store contact.
        docs.list_receipts.assert_called_once_with("uningested")

    def test_bare_local_not_in_store_is_silent(
        self, tmp_path: pathlib.Path, capsys: "pytest.CaptureFixture[str]"
    ) -> None:
        docs = mock.MagicMock()
        docs.list_receipts.return_value = ["other.pdf"]
        with mock.patch("os.getcwd", return_value=str(tmp_path)):
            maybe_warn_collision(docs, "unassociated", "r.pdf", self._bare_local_ref())

        assert capsys.readouterr().err == ""
        docs.list_receipts.assert_called_once_with("unassociated")

    def test_pathlike_local_makes_no_store_call(
        self, tmp_path: pathlib.Path, capsys: "pytest.CaptureFixture[str]"
    ) -> None:
        docs = mock.MagicMock()
        docs.list_receipts.return_value = ["x.pdf"]
        ref = ReceiptRef(
            Source.LOCAL, "/abs/x.pdf", pathlib.Path("/abs/x.pdf"), "x.pdf"
        )
        maybe_warn_collision(docs, "uningested", "/abs/x.pdf", ref)

        assert capsys.readouterr().err == ""
        docs.list_receipts.assert_not_called()

    def test_store_ref_makes_no_store_call(
        self, tmp_path: pathlib.Path, capsys: "pytest.CaptureFixture[str]"
    ) -> None:
        docs = mock.MagicMock()
        ref = ReceiptRef(Source.STORE, "s.pdf", pathlib.Path("s.pdf"), "s.pdf")
        maybe_warn_collision(docs, "uningested", "s.pdf", ref)

        docs.list_receipts.assert_not_called()
        assert capsys.readouterr().err == ""


# ===========================================================================
# preview_receipt(): uses the loaded bytes, no second fetch
# ===========================================================================


class TestPreviewReceipt:
    def test_preview_writes_loaded_bytes_to_dir(
        self, tmp_path: pathlib.Path, monkeypatch: "pytest.MonkeyPatch"
    ) -> None:
        opened: list[pathlib.Path] = []

        def _fake_open(dest: pathlib.Path) -> None:
            opened.append(dest)

        monkeypatch.setattr(documents_client, "open_document", _fake_open)

        fetched = FetchedReceipt(SAMPLE_BYTES, SAMPLE_TS, "r.pdf")
        documents_client.preview_receipt(fetched, tmp_path)

        dest = tmp_path / "r.pdf"
        assert dest.exists()
        assert dest.read_bytes() == SAMPLE_BYTES
        assert opened == [dest]

    def test_preview_makes_no_fetch(self, tmp_path: pathlib.Path) -> None:
        # Preview accepts the FetchedReceipt itself, so there is no DocumentsClient
        # at all to fetch from: the double-fetch for a local receipt is gone.
        with mock.patch(
            "beanhand.client.server.documents.open_document", mock.MagicMock()
        ) as fake_open:
            documents_client.preview_receipt(
                FetchedReceipt(b"x", 1.0, "a.pdf"), tmp_path
            )

        fake_open.assert_called_once_with(tmp_path / "a.pdf")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
