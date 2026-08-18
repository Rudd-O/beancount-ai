import json
import os
from pathlib import Path
from typing import ClassVar

CONF_DEFAULT = Path.home() / ".config" / "bean-ai.json"


class BeancountConfiguration:
    """Configuration representing a Beancount setup.

    Attributes:
        main_file: full path to the main Beancount file.
        ingestion_destination_file: file name (or path relative to the main Beancount file),
                                    to which transactions ingested from receipts will be appended;
                                    if left empty, this will default to your main_file.
        account_list_file: file name containing a listing of Beancount accounts to consider when
                           making a transaction during the ingestion process.
    """

    main_file: Path
    ingestion_destination_file: Path | None
    account_list_file: Path

    @property
    def ingestion_destination_path(self) -> Path:
        if self.ingestion_destination_file is None:
            return self.main_file
        return self.main_file.parent / self.ingestion_destination_file

    @property
    def main_folder(self) -> Path:
        return self.main_file.parent


class Configuration:
    """Configuration loaded from a bean-ai JSON config file.

    Singleton that caches its first loaded instance at the class level.
    Use :meth:`load` to retrieve or initialise it.

    Attributes:
        target_vm: Name of the Qubes VM where bean-ai-server runs (omit to launch bean-ai-server locally).
        beancount: an instance of BeancountConfiguration
    """

    instance: ClassVar["Configuration | None"] = None
    cfg_path: ClassVar[Path | None] = None  # which file was actually loaded
    target_vm: str | None
    beancount: BeancountConfiguration

    def __init__(self) -> None:
        raise NotImplementedError("Use Configuration.load() to obtain an instance")

    @classmethod
    def _get_cfg_path(cls, override: str | None) -> Path:
        """Return the config file path, resolving overrides in order of priority.

        Priority (highest → lowest):
            1. ``--config`` CLI argument
            2. ``BEAN_AI_CONFIG`` environment variable
            3. Default ``~/.config/bean-ai.json``
        """
        if override:
            return Path(override)
        env_cfg = os.environ.get("BEAN_AI_CONFIG")
        if env_cfg:
            return Path(env_cfg)
        return CONF_DEFAULT

    @classmethod
    def load(cls, override: str | None = None) -> "Configuration":
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
        instance.target_vm = data.get("target_vm", None)
        instance.beancount = BeancountConfiguration()
        instance.beancount.main_file = Path(data["beancount"]["main_file"])
        tdf = data["beancount"].get("ingestion_destination_file", None)
        if tdf is not None:
            tdf = Path(tdf)
        instance.beancount.ingestion_destination_file = tdf
        instance.beancount.account_list_file = Path(
            data["beancount"]["account_list_file"]
        )
        cls.instance = instance
        return cls.instance
