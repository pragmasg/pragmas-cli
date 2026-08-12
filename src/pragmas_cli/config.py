"""Local credential storage — a beta key from `pragmas login`, nothing else.

No password, no OAuth token, no plan/billing state: this is deliberately as
simple as the beta-key model it wraps (see pragmas-sdk's CONTRACT.md).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

DEFAULT_BASE_URL = "https://api.pragmas.io"


def config_dir() -> Path:
    override = os.environ.get("PRAGMAS_CONFIG_DIR")
    return Path(override) if override else Path.home() / ".pragmas"


def config_path() -> Path:
    return config_dir() / "credentials.json"


def load_config() -> dict[str, Any]:
    path = config_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_config(**fields: Any) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = load_config()
    data.update({k: v for k, v in fields.items() if v is not None})
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def get_base_url() -> str:
    return os.environ.get("PRAGMAS_BASE_URL") or load_config().get("base_url") or DEFAULT_BASE_URL


def get_beta_key() -> str | None:
    return os.environ.get("PRAGMAS_BETA_KEY") or load_config().get("beta_key")
