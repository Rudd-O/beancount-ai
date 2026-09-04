#!/usr/bin/env python3
"""Tests for the :class:`FileGuard` external-modification guard in beanfiles.

``FileGuard`` lets the file-modifying commands tell whether a Beancount file
changed on disk after they read it, so they can refuse to clobber edits the
user made while bean-ai was talking to the LLM.
"""

import os
import pathlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent.parent))

from beancount_ai.client.beanfiles import (
    FileGuard,
    FileModifiedError,
)


class TestTake:
    def test_take_returns_guard_for_existing_file(
        self, tmp_path: Path
    ) -> None:
        f = tmp_path / "a.bean"
        f.write_text("content", encoding="utf-8")
        assert isinstance(FileGuard.take(f), FileGuard)

    def test_take_raises_for_missing_file(
        self, tmp_path: Path
    ) -> None:
        # A caller taking a guard has just read the file, so a missing file is a
        # genuine error, not a state to swallow: ``take`` raises, it never
        # returns ``None``.
        with pytest.raises(FileNotFoundError):
            FileGuard.take(tmp_path / "does-not-exist.bean")


class TestVerify:
    def test_verify_passes_when_unchanged(self, tmp_path: Path) -> None:
        f = tmp_path / "a.bean"
        f.write_text("content", encoding="utf-8")
        FileGuard.take(f).verify()  # must not raise

    def test_verify_detects_content_change(self, tmp_path: Path) -> None:
        f = tmp_path / "a.bean"
        f.write_text("original", encoding="utf-8")
        guard = FileGuard.take(f)
        f.write_text("original, now edited", encoding="utf-8")
        with pytest.raises(FileModifiedError):
            guard.verify()

    def test_verify_ignores_mtime_touch(self, tmp_path: Path) -> None:
        """Touching the timestamp (same bytes) must NOT be flagged as a modification."""
        f = tmp_path / "a.bean"
        f.write_text("same bytes", encoding="utf-8")
        guard = FileGuard.take(f)
        # Force a distinct mtime via os.utime so the inode timestamp actually
        # changes while the content does not.
        past = os.stat(f).st_mtime
        os.utime(f, (past + 1000, past + 1000))
        guard.verify()  # must not raise (content unchanged)

    def test_verify_detects_deletion(self, tmp_path: Path) -> None:
        f = tmp_path / "a.bean"
        f.write_text("was here", encoding="utf-8")
        guard = FileGuard.take(f)
        f.unlink()
        with pytest.raises(FileModifiedError):
            guard.verify()

    def test_verify_reports_path_in_message(self, tmp_path: Path) -> None:
        f = tmp_path / "my ledger.bean"
        f.write_text("a", encoding="utf-8")
        guard = FileGuard.take(f)
        f.write_text("a b", encoding="utf-8")
        with pytest.raises(FileModifiedError) as exc:
            guard.verify()
        assert str(f) in str(exc.value)


class TestError:
    def test_error_is_a_catchable_exception(self) -> None:
        assert issubclass(FileModifiedError, Exception)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
