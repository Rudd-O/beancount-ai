import json
from pathlib import Path
from typing import Any, ClassVar

from beanhand.config import ConfigBase


class Configuration(ConfigBase):
    """Base class for receipt storage backend configurations.

    Two concrete backends exist: WebDAV (``WebDAVConfiguration``)
    and local files (``LocalFileConfiguration``).  The active
    backend is selected by the ``documents.backend`` key in the config file
    (``"webdav"`` or ``"local"``); see :meth:`Configuration.load` for the
    fallback rules used when the key is absent.
    """

    instance: ClassVar["Configuration | None"] = None
    cfg_path: ClassVar[Path | None] = None  # which file was actually loaded

    def uningested_location_name(self) -> str:
        """Human-readable name of the uningested receipts location."""
        raise NotImplementedError

    def unassociated_location_name(self) -> str:
        """Human-readable name of the unassociated receipts location."""
        raise NotImplementedError

    @classmethod
    def load(cls, override: str | None | None = None) -> "Configuration":
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
        if not isinstance(data, dict):
            raise ValueError("config file must contain a JSON object")
        try:
            section = data["documents"]
        except KeyError:
            raise ValueError("config file must contain a documents section")
        if not isinstance(section, dict):
            raise ValueError("config file documents section must be a dictionary")
        instance = cls._load_documents(section)
        cls.instance = instance
        return cls.instance

    _LOCAL_REQUIRED = (
        "uningested_receipts_folder",
        "unassociated_receipts_folder",
    )
    _WEBDAV_REQUIRED = (
        "base_url",
        "uningested_receipts_subfolder",
        "unassociated_receipts_subfolder",
    )

    @classmethod
    def _load_documents(cls, data: dict[Any, Any]) -> Configuration:
        """Build the receipt backend configuration from the ``documents`` section.

        The backend is selected by the ``backend`` key (``"local"`` or
        ``"webdav"``).  When the key is absent (legacy configs), the backend
        is inferred from the fields present: the local backend is chosen when
        both of its mandatory keys (``uningested_receipts_folder`` and
        ``unassociated_receipts_folder``) are present, otherwise the WebDAV
        backend is used.

        Each backend has its own mandatory keys, and a ``ValueError`` is
        raised if any of them is missing:

        * local:  ``uningested_receipts_folder``,
          ``unassociated_receipts_folder``
        * webdav: ``base_url``, ``uningested_receipts_subfolder``,
          ``unassociated_receipts_subfolder``
        """
        backend = str(data.get("backend", ""))
        if backend == "":
            if all(k in data for k in cls._LOCAL_REQUIRED):
                backend = "local"
            else:
                backend = "webdav"

        if backend == "local":
            missing = [k for k in cls._LOCAL_REQUIRED if k not in data]
            if missing:
                raise ValueError(
                    "the local backend requires the mandatory key(s): "
                    + ", ".join(missing)
                )
            local_cfg = LocalFileConfiguration()
            local_cfg.uningested_receipts_folder = Path(
                str(data["uningested_receipts_folder"])
            )
            local_cfg.unassociated_receipts_folder = Path(
                str(data["unassociated_receipts_folder"])
            )
            return local_cfg
        if backend == "webdav":
            missing = [k for k in cls._WEBDAV_REQUIRED if k not in data]
            if missing:
                raise ValueError(
                    "the webdav backend requires the mandatory key(s): "
                    + ", ".join(missing)
                )
            webdav_cfg = WebDAVConfiguration()
            webdav_cfg.username = str(data.get("username", ""))
            webdav_cfg.password = str(data.get("password", ""))
            webdav_cfg.base_url = str(data["base_url"])
            webdav_cfg.uningested_receipts_subfolder = str(
                data["uningested_receipts_subfolder"]
            )
            webdav_cfg.unassociated_receipts_subfolder = str(
                data["unassociated_receipts_subfolder"]
            )
            return webdav_cfg
        raise ValueError(f"unknown documents backend: {backend!r}")


class LocalFileConfiguration(Configuration):
    """Configuration for the local file storage backend.

    Receipts live in two independent folders on the server's filesystem:
    one for uningested receipts (awaiting conversion into transactions) and
    one for unassociated receipts (awaiting association with an existing
    transaction).  Each folder is configured as a full path; there is no
    shared base directory (that is a WebDAV-only concept).  The two folders
    must be distinct.

    Attributes:
        uningested_receipts_folder: Full path to the folder containing
            receipts to be ingested.
        unassociated_receipts_folder: Full path to the folder containing
            receipts to be associated.
    """

    uningested_receipts_folder: Path
    unassociated_receipts_folder: Path

    def receipts_uningested_folder(self) -> Path:
        return self.uningested_receipts_folder

    def receipts_unassociated_folder(self) -> Path:
        return self.unassociated_receipts_folder

    def uningested_location_name(self) -> str:
        return f"local folder {self.uningested_receipts_folder}"

    def unassociated_location_name(self) -> str:
        return f"local folder {self.unassociated_receipts_folder}"


class WebDAVConfiguration(Configuration):
    """Configuration for WebDAV storage backend.

    Attributes:
        username: User name needed to connect to WebDAV server.
        password: Password needed to connect to WebDAV server.
        base_url: URL to WebDAV server containing receipts.
        uningested_receipts_subfolder: Folder in WebDAV server containing receipts to be ingested.
        receipts_association_folder: Folder in WebDAV server containing receipts to be associated.
    """

    username: str
    password: str
    base_url: str
    uningested_receipts_subfolder: str
    unassociated_receipts_subfolder: str

    def receipts_ingestion_url(self) -> str:
        s = self.base_url
        if not s.endswith("/"):
            s += "/"
        s += self.uningested_receipts_subfolder
        return s

    def receipts_association_url(self) -> str:
        s = self.base_url
        if not s.endswith("/"):
            s += "/"
        s += self.unassociated_receipts_subfolder
        return s

    def uningested_location_name(self) -> str:
        return f"WebDAV URL {self.receipts_ingestion_url()}"

    def unassociated_location_name(self) -> str:
        return f"WebDAV URL {self.receipts_association_url()}"
