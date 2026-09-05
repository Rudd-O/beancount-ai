#!/usr/bin/env python3
"""Tests for preserving the server-side modification time across a fetch.

Covers the wire format used by ``beanai.Fetch`` (a single JSONL metadata line
``{"timestamp": float}`` followed by the raw receipt bytes) and the client-side
partitioning + ``save_receipt`` mtime restoration.
"""

import argparse
import io
import json
import pathlib
import sys
from typing import IO
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent.parent))

from beancount_ai.client.server import RemoteVM, save_receipt
from beancount_ai.server.commands.fetch import run as do_fetch
from beancount_ai.structs import FetchedReceipt

# ===========================================================================
# Server wire format
# ===========================================================================


class TestServerFetchWireFormat:
    def _run(
        self,
        data: bytes,
        timestamp: float,
        captured_stdout: io.BytesIO,
    ) -> None:
        backend = mock.MagicMock()
        backend.read.return_value = FetchedReceipt(data, timestamp)
        cfg = mock.MagicMock()
        cfg.documents.uningested_location_name.return_value = "loc"
        args = argparse.Namespace(filename="r.png".encode("utf-8").hex())
        with (
            mock.patch(
                "beancount_ai.server.commands.fetch.make_receipt_backend",
                return_value=backend,
            ),
            mock.patch.object(sys, "stdout", _FakeStdout(captured_stdout)),
        ):
            do_fetch(cfg, args)

    def test_metadata_line_then_raw_bytes(self) -> None:
        body = b"%PDF-1.4 raw"
        ts = 1700000000.5
        captured = io.BytesIO()
        self._run(body, ts, captured)

        out = captured.getvalue()
        meta, sep, data = out.partition(b"\n")
        assert sep == b"\n"
        assert data == body
        assert json.loads(meta) == {"timestamp": ts}

    def test_body_may_contain_newlines(self) -> None:
        body = b"line1\nline2\nline3"
        ts = 123.0
        captured = io.BytesIO()
        self._run(body, ts, captured)

        meta, sep, data = captured.getvalue().partition(b"\n")
        assert sep == b"\n"
        assert data == body
        assert isinstance(json.loads(meta)["timestamp"], float)


class _FakeStdout:
    """Stand-in for sys.stdout that exposes an in-memory ``.buffer``."""

    def __init__(self, buffer: io.BytesIO) -> None:
        self.buffer = buffer

    def write(self, s: str) -> int:  # pragma: no cover - only .buffer is used
        raise AssertionError("text-mode write should not be used")


# ===========================================================================
# Client parsing + mtime restoration
# ===========================================================================


class TestClientFetchParsing:
    def test_fetch_receipt_partitions_metadata(self) -> None:
        body = b"%PDF-1.4 raw"
        ts = 1700000000.5
        wire = json.dumps({"timestamp": ts}).encode("utf-8") + b"\n" + body

        inner_stdout = io.BytesIO(wire)

        def _fake_call(
            action: str, arg: str | None = None
        ) -> tuple[list[str], mock.MagicMock, IO[bytes], IO[bytes]]:
            proc = mock.MagicMock()
            proc.stdout = inner_stdout
            proc.wait.return_value = 0
            return (["cmd"], proc, mock.MagicMock(), inner_stdout)

        vm = RemoteVM(None)
        with mock.patch.object(vm, "_call", side_effect=_fake_call):
            fetched = vm.fetch_receipt("r.png")

        assert isinstance(fetched, FetchedReceipt)
        assert fetched.data == body
        assert fetched.timestamp == ts


class TestSaveReceipt:
    def test_saves_bytes_and_restores_mtime(self, tmp_path: pathlib.Path) -> None:
        dest = tmp_path / "sub" / "r.png"
        body = b"%PDF-1.4 raw"
        ts = 1700000000.5
        save_receipt(dest, FetchedReceipt(body, ts))

        stat = dest.stat()  # stat before reading; read_bytes() would bump atime
        assert stat.st_mtime == ts
        assert stat.st_atime == ts
        assert dest.read_bytes() == body

    def test_creates_parent_dirs(self, tmp_path: pathlib.Path) -> None:
        dest = tmp_path / "a" / "b" / "c" / "r.png"
        save_receipt(dest, FetchedReceipt(b"x", 1.0))
        assert dest.exists()

    def test_preserves_fractional_timestamp(self, tmp_path: pathlib.Path) -> None:
        dest = tmp_path / "r.png"
        ts = 1700000000.123
        save_receipt(dest, FetchedReceipt(b"x", ts))
        assert dest.stat().st_mtime == ts
