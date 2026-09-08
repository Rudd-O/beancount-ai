import json
from pathlib import Path
from typing import Any, ClassVar

from beanhand.config import ConfigBase


class Configuration(ConfigBase):
    """Configuration loaded from a beanhand JSON config file.

    Singleton that caches its first loaded instance at the class level.
    Use :meth:`load` to retrieve or initialise it.

    Attributes:
        api_url: Base URL of the LLM API instance for receipt processing.
            When absent, the OpenAI API itself is used.
        token: API token for authenticating with the LLM API instance.
        model_name: Model name to use via the LLM API instance.
    """

    instance: ClassVar["Configuration | None"] = None
    cfg_path: ClassVar[Path | None] = None  # which file was actually loaded
    api_url: str | None
    token: str
    model_name: str

    @classmethod
    def _load_ai(cls, data: dict[str, Any]) -> Configuration:
        missing = [k for k in ("token", "model_name") if k not in data]
        if missing:
            raise ValueError(
                "the ai section requires the mandatory key(s): " + ", ".join(missing)
            )
        ai_cfg = cls()
        ai_cfg.api_url = data.get("api_url")
        ai_cfg.token = str(data["token"])
        ai_cfg.model_name = str(data["model_name"])
        return ai_cfg

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
            section = data["ai"]
        except KeyError:
            raise ValueError("config file must contain an section")
        if not isinstance(section, dict):
            raise ValueError("config file ai section must be a dictionary")
        instance = cls._load_ai(section)
        cls.instance = instance
        return cls.instance
