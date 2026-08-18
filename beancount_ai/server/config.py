import json
import os
from pathlib import Path
from typing import ClassVar

CONF_DEFAULT = Path.home() / ".config" / "bean-ai.json"


class DocumentSourcesConfiguration:
    pass


class WebDAVDocumentSourcesConfiguration(DocumentSourcesConfiguration):
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


class AIConfiguration:
    """Configuration for AI backend.

    Attributes:
        api_url: Base URL of the LLM API instance for receipt processing.
        token: API token for authenticating with the LLM API instance.
        model_name: Model name to use via the LLM API instance.
    """

    api_url: str
    token: str
    model_name: str


class Configuration:
    """Configuration loaded from a bean-ai JSON config file.

    Singleton that caches its first loaded instance at the class level.
    Use :meth:`load` to retrieve or initialise it.

    Attributes:
        ai: an instance of AIConfiguration
        documents: an instance of WebDAVDocumentSourcesConfiguration
    """

    instance: ClassVar["Configuration | None"] = None
    cfg_path: ClassVar[Path | None] = None  # which file was actually loaded

    ai: AIConfiguration
    documents: DocumentSourcesConfiguration

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
        instance = cls.__new__(cls)
        instance.ai = AIConfiguration()
        instance.ai.api_url = data["ai"]["api_url"]
        instance.ai.token = data["ai"]["token"]
        instance.ai.model_name = data["ai"]["model_name"]
        instance.documents = WebDAVDocumentSourcesConfiguration()
        instance.documents.username = data["documents"]["username"]
        instance.documents.password = data["documents"]["password"]
        instance.documents.base_url = data["documents"]["base_url"]
        instance.documents.uningested_receipts_subfolder = data["documents"][
            "uningested_receipts_subfolder"
        ]
        instance.documents.unassociated_receipts_subfolder = data["documents"][
            "unassociated_receipts_subfolder"
        ]
        cls.instance = instance
        return cls.instance
