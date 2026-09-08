import fcntl
import json
import sys
import warnings
from pathlib import Path
from types import TracebackType
from typing import IO, ClassVar

from beanhand.config import ConfigBase


class BeancountConfiguration:
    """Configuration representing a Beancount setup.

    Attributes:
        main_file: full path to the main Beancount file.
        ingestion_destination_file: file name (or path relative to the main Beancount file),
                                    to which transactions ingested from receipts will be appended;
                                    if left empty, this will default to your main_file.
    """

    main_file: Path
    ingestion_destination_file: Path | None
    _lock_fh: IO[bytes] | None

    def __init__(
        self,
        main_file: Path,
        ingestion_destination_file: Path | None = None,
    ) -> None:
        self.main_file = main_file
        self.ingestion_destination_file = ingestion_destination_file
        self._lock_fh = None
        # Lock right away, at instantiation, so that no caller can forget to do it.
        self.lock()

    def __del__(self) -> None:
        fh = getattr(self, "_lock_fh", None)
        if fh is not None:
            self.unlock()

    def lock(self) -> None:
        """Acquire an exclusive advisory lock on the main Beancount file.

        Called from :meth:`__init__`; idempotent -- a second call on an
        already-locked instance is a no-op.  The lock is held until :meth:`unlock`
        is called or the process exits, which (for the one-shot CLI) means it
        is held for the duration of the enclosing subcommand, so that
        concurrent invocations of data-modifying subcommands queue up one
        behind the other instead of trampling each other's data.  A
        non-blocking attempt is made first; if another process already holds
        the lock, a message is printed to standard error and the lock is then
        attempted again, this time blocking (hanging) until it is released.

        The open file handle is kept alive on the instance so that the lock
        (tied to the open file description) is not accidentally released by
        garbage collection.
        """
        if self._lock_fh is not None:
            return
        fh = open(self.main_file, "rb")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, InterruptedError):
            print(
                f"Beancount data files ({self.main_file}) are locked by another process; "
                "waiting until the lock is released ...",
                file=sys.stderr,
            )
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        self._lock_fh = fh

    def unlock(self) -> None:
        """Release the advisory lock, if it was acquired."""
        if self._lock_fh is not None:
            fcntl.flock(self._lock_fh.fileno(), fcntl.LOCK_UN)
            self._lock_fh.close()
            self._lock_fh = None

    def __enter__(self) -> "BeancountConfiguration":
        self.lock()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.unlock()

    @property
    def ingestion_destination_path(self) -> Path:
        if self.ingestion_destination_file is None:
            return self.main_file
        return self.main_file.parent / self.ingestion_destination_file

    @property
    def main_folder(self) -> Path:
        return self.main_file.parent


class Configuration(ConfigBase):
    """Configuration loaded from a beanhand JSON config file.

    Singleton that caches its first loaded instance at the class level.
    Use :meth:`load` to retrieve or initialise it.

    Attributes:
        documents_target_vm: Name of the Qubes VM where
            beanhand-documents-server runs; ``null`` launches it locally.
        ai_target_vm: Name of the Qubes VM where beanhand-ai-server runs;
            ``null`` launches it locally.
        beancount: an instance of BeancountConfiguration
    """

    instance: ClassVar["Configuration | None"] = None
    cfg_path: ClassVar[Path | None] = None  # which file was actually loaded
    documents_target_vm: str | None
    ai_target_vm: str | None
    beancount: BeancountConfiguration

    def __init__(self) -> None:
        raise NotImplementedError("Use Configuration.load() to obtain an instance")

    @classmethod
    def load(cls, override: str | None = None) -> "Configuration":  # noqa: C901
        """Load and cache the config from the resolved path.

        If called multiple times, only the *first* invocation's resolution is used;
        subsequent calls return the cached result (prevents a user from accidentally
        reloading with different paths within one process).
        """
        if cls.instance is not None:
            return cls.instance

        fp = cls._get_cfg_path(override)
        cls.cfg_path = fp
        with open(fp) as fh:
            data = json.load(fh)
        instance = cls.__new__(cls)

        catchall_target_vm = data.get("target_vm", None)
        if catchall_target_vm is not None:
            if not isinstance(catchall_target_vm, str):
                raise ValueError(
                    f"target_vm in must be a VM name (non-empty string) when using its `qubes` backend"
                )
            warnings.warn(
                f"You are using configuration key target_vm."
                "  This will be removed in the future."
                "  Migrate to specify ai:backend:vm and documents:backend:vm."
            )
        for sectname, attr in [
            ("ai", "ai_target_vm"),
            ("documents", "documents_target_vm"),
        ]:
            # Set it to default to None first.
            setattr(instance, attr, None)
            try:
                section = data[sectname]
            except KeyError:
                if catchall_target_vm is None:
                    raise ValueError(f"configuration must include a {sectname} section")
                else:
                    section = {"backend": "qubes", "vm": catchall_target_vm}
            if not isinstance(section, dict):
                raise ValueError(
                    f"the {sectname} section in the configuration must be a dictionary"
                )

            if "vm" in section or section.get("backend") == "qubes":
                invalid_keys = [b for b in section if b not in ["backend", "vm"]]
                if invalid_keys:
                    raise ValueError(
                        f"if vm is specified in {sectname}, then other configuration keys ({invalid_keys}) may not be specified"
                    )
                vm_u = section.get("vm")
                if not isinstance(vm_u, str) or not vm_u:
                    raise ValueError(
                        f"vm in section {sectname} must be a VM name (non-empty string) when using its `qubes` backend"
                    )
                setattr(instance, attr, vm_u)

        bean = data["beancount"]
        if "account_list_file" in bean:
            raise ValueError(
                "beancount.account_list_file is no longer used and will "
                "be ignored; mark your accounts with beanhand-include / beanhand-exclude "
                "in the ledger"
            )
        tdf = bean.get("ingestion_destination_file", None)
        if tdf is not None:
            tdf = Path(tdf)
        instance.beancount = BeancountConfiguration(
            main_file=Path(bean["main_file"]),
            ingestion_destination_file=tdf,
        )
        cls.instance = instance
        return cls.instance
