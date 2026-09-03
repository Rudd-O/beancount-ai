#!/usr/bin/env python3
"""Unit tests for BeancountConfiguration advisory file locking (posix flock)."""

import fcntl
import os
import pathlib
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent.parent))

from beancount_ai.client.beanfiles import write_beancount_file
from beancount_ai.client.config import BeancountConfiguration

# ===========================================================================
# Helpers
# ===========================================================================


def _make_beancount_configuration(folder: pathlib.Path) -> BeancountConfiguration:
    main = folder / "main.bean"
    main.write_text('2026-01-01 * "Seed" "Start"\n  Expenses:Misc  1.00 USD\n')
    (folder / "accounts.txt").write_text("Expenses:Misc\n")
    return BeancountConfiguration(
        main_file=main,
        account_list_file=folder / "accounts.txt",
        ingestion_destination_file=None,
    )


def _lock_probe(
    path: Path,
) -> bool:
    """Return True if an exclusive lock can be taken non-blockingly on *path*.

    Opens the path fresh (a distinct open file description) and attempts a
    non-blocking exclusive flock; releases before returning.
    """
    with open(path, "rb") as fh:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, InterruptedError):
            return False
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        return True


# ===========================================================================
# lock / unlock
# ===========================================================================


class TestLock:
    def test_lock_holds_exclusive_lock(self, tmp_path: pathlib.Path) -> None:
        bc = _make_beancount_configuration(tmp_path)
        assert not _lock_probe(bc.main_file)

    def test_unlock_releases_lock(self, tmp_path: pathlib.Path) -> None:
        bc = _make_beancount_configuration(tmp_path)
        bc.unlock()
        assert _lock_probe(bc.main_file)

    def test_del_releases_lock(self, tmp_path: pathlib.Path) -> None:
        bc = _make_beancount_configuration(tmp_path)
        assert not _lock_probe(bc.main_file)
        mf = bc.main_file
        del bc
        assert _lock_probe(mf)

    def test_lock_is_idempotent(
        self, tmp_path: pathlib.Path, capsys: "pytest.CaptureFixture[str]"
    ) -> None:
        """A second lock() on an already-locked instance is a no-op: it does not
        re-acquire, and the already-locked file handle does not trigger the
        'locked by another process' path (which would hang)."""
        bc = _make_beancount_configuration(tmp_path)
        bc.lock()
        bc.unlock()
        bc.unlock()
        assert _lock_probe(bc.main_file)
        assert "locked by another process" not in capsys.readouterr().err

    def test_lock_stored_on_instance(self, tmp_path: pathlib.Path) -> None:
        """The open file is kept alive on the instance so the lock cannot be
        garbage-collected out from under the process."""
        bc = _make_beancount_configuration(tmp_path)
        fh = bc._lock_fh  # pyright: ignore[reportPrivateUsage]
        assert fh is not None
        try:
            assert not _lock_probe(bc.main_file)
        finally:
            bc.unlock()
        assert bc._lock_fh is None  # pyright: ignore[reportPrivateUsage]

    def test_locked_file_remains_readable(self, tmp_path: pathlib.Path) -> None:
        """The lock is taken on a read-mode open of the main file; the file
        itself must remain readable while locked."""
        bc = _make_beancount_configuration(tmp_path)
        try:
            content = bc.main_file.read_text(encoding="utf-8")
            assert "Seed" in content
        finally:
            bc.unlock()


class TestLockContendsWithOtherProcess:
    """Cross-process behaviour: a non-blocking probe from another process must
    see our lock, and unlocking it again must hang (blocking regrab) until the
    original lock goes away.  We only test the probe side to keep the suite
    non-serial; the blocking regrab path is covered by the two-phase design
    asserted in test_lock_then_blocking_regrab_via_subprocess below."""

    def test_other_process_sees_lock(self, tmp_path: pathlib.Path) -> None:
        bc = _make_beancount_configuration(tmp_path)
        out = subprocess.run(
            [
                sys.executable,
                "-c",
                "import fcntl,sys;"
                "fh=open(sys.argv[1],'rb');\n"
                "try:\n"
                "    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
                "except BlockingIOError:\n"
                "    print('blocked')\n"
                "else:\n"
                "    print('free')\n"
                "finally:\n"
                "    fh.close()",
                str(bc.main_file),
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert out.returncode == 0
        assert out.stdout.strip() == "blocked"


def _blocking_regrab_program(path: str) -> str:
    """Source for a subprocess that: (1) probes non-blockingly expecting a
    blocked lock, (2) prints a marker to parent-stdout, then (3) flocks
    blockingly and (4) prints done (only possible after the holder releases).
    A watchdog (via subprocess timeout) would kill it if step 3 never returned.
    """
    return (
        "import fcntl,sys\n"
        "fh=open(sys.argv[1],'rb')\n"
        "try:\n"
        "    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
        "except BlockingIOError:\n"
        "    print('phase1-blocked', flush=True)\n"
        "else:\n"
        "    print('phase1-free', flush=True); fh.close(); sys.exit(2)\n"
        "print('phase2-start', flush=True)\n"
        "fcntl.flock(fh.fileno(), fcntl.LOCK_EX)\n"
        "print('phase2-done', flush=True)\n"
        "fh.close()\n"
    )


class TestLockBlockingRegrab:
    def test_lock_then_blocking_regrab_via_subprocess(
        self, tmp_path: pathlib.Path
    ) -> None:
        """Hold the lock in this process; a child process following the exact
        two-phase algorithm of BeancountConfiguration.lock() must first observe
        the contention (non-blocking) and then, on the blocking retry, hang
        until this process releases the lock, after which it finishes."""
        bc = _make_beancount_configuration(tmp_path)
        try:
            proc = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    _blocking_regrab_program(str(bc.main_file)),
                    str(bc.main_file),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            assert proc.stdout is not None
            line1 = proc.stdout.readline().strip()
            line2 = proc.stdout.readline().strip()
            assert line1 == "phase1-blocked", line1
            assert line2 == "phase2-start", line2
            # The child is now sitting in the blocking flock() call.
            # Release our lock; the child must then complete.
            bc.unlock()
            out, _ = proc.communicate(timeout=10)
            assert proc.returncode == 0
            assert "phase2-done" in out
        finally:
            bc.unlock()


class TestContextManager:
    def test_enter_exit_acquires_and_releases(self, tmp_path: pathlib.Path) -> None:
        bc = _make_beancount_configuration(tmp_path)
        with bc:
            assert not _lock_probe(bc.main_file)
        assert _lock_probe(bc.main_file)

    def test_exit_releases_even_on_exception(self, tmp_path: pathlib.Path) -> None:
        bc = _make_beancount_configuration(tmp_path)
        exit_code = False
        try:
            with bc:
                assert not _lock_probe(bc.main_file)
                raise ValueError("boom")
        except ValueError:
            exit_code = True
        assert exit_code
        assert _lock_probe(bc.main_file)


# ===========================================================================
# write_beancount_file
# ===========================================================================


class TestWriteBeancountFile:
    def test_writes_content(self, tmp_path: pathlib.Path) -> None:
        target = tmp_path / "ledger.bean"
        write_beancount_file(target, '2026-01-01 * "X" "Y"\n')
        assert target.read_text(encoding="utf-8") == '2026-01-01 * "X" "Y"\n'

    def test_overwrites_existing(self, tmp_path: pathlib.Path) -> None:
        target = tmp_path / "ledger.bean"
        target.write_text("old content\n")
        write_beancount_file(target, "new content\n")
        assert target.read_text(encoding="utf-8") == "new content\n"

    def test_flushes_and_syncs_to_disk(self, tmp_path: pathlib.Path) -> None:
        """The file must be fully flushed *and* fsync'd to disk before
        write_beancount_file returns."""
        target = tmp_path / "ledger.bean"
        real_fsync = os.fsync
        syscalls: dict[str, bool] = {"flush": False, "fsync": False}

        real_open = open

        class _SpyFile:
            """Wraps a real file and records flush()/fileno() usage."""

            def __init__(self) -> None:
                self._real = real_open(target, "w", encoding="utf-8")

            def __enter__(self) -> "_SpyFile":
                return self

            def __exit__(self, *exc: object) -> None:
                self._real.close()

            def write(self, data: str) -> int:
                return self._real.write(data)

            def flush(self) -> None:
                syscalls["flush"] = True
                self._real.flush()

            def fileno(self) -> int:
                return self._real.fileno()

        def spy_fsync(fd: int) -> None:
            syscalls["fsync"] = True
            real_fsync(fd)

        with mock.patch("builtins.open", return_value=_SpyFile()):
            with mock.patch(
                "beancount_ai.client.beanfiles.os.fsync", side_effect=spy_fsync
            ):
                write_beancount_file(target, "synced\n")

        assert syscalls["flush"]
        assert syscalls["fsync"]
        assert target.read_text(encoding="utf-8") == "synced\n"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
