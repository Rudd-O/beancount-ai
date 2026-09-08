import os
import warnings
from pathlib import Path

CONF_DEFAULT = Path.home() / ".config" / "beanhand.json"
CONF_FALLBACK = Path.home() / ".config" / "bean-ai.json"


class ConfigBase:
    @classmethod
    def _get_cfg_path(cls, override: str | None) -> Path:
        """Return the config file path, resolving overrides in order of priority.

        Priority (highest → lowest):
            1. ``--config`` CLI argument
            2. ``BEANHAND_CONFIG`` environment variable
            3. Default ``~/.config/beanhand.json``
            4. Fallback ``~/.config/bean-ai.json``
        """
        if override:
            return Path(override)
        env_cfg = os.environ.get("BEANHAND_CONFIG")
        if env_cfg:
            return Path(env_cfg)
        if os.path.exists(CONF_FALLBACK) and not os.path.exists(CONF_DEFAULT):
            warnings.warn(
                f"You are using fallback configuration file {CONF_FALLBACK}."
                "  Fallback support will be removed in the future."
                "  The new default configuration file lives at {CONF_DEFAULT}."
            )
            return CONF_FALLBACK
        return CONF_DEFAULT
