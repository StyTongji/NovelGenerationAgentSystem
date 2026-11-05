"""Resolve a provider's named key consistently for readiness and API calls."""
from __future__ import annotations

import os

from dotenv import dotenv_values


def read_api_key(env_name: str) -> str:
    # An explicitly empty process variable intentionally disables the key.
    if env_name in os.environ:
        return os.environ[env_name].strip()
    return (dotenv_values(".env").get(env_name) or "").strip()
