"""
Env loader — reads ~/.company-os/.env at startup
=================================================
A minimal .env parser with no external dependencies. Called at the top of
main.py and test_flow.py before any anthropic client is instantiated.

Only sets variables that are not already set in os.environ — so a real
system environment variable always wins over the .env file.
"""

from __future__ import annotations

import os
from pathlib import Path

# Chunk 1a.1 relocated get_vault_dir() to core/config.py. This module re-exports
# the name so legacy imports (webapp.services, main.py, test_flow.py,
# comprehensive_demo.py, tests/conftest.py) keep working without edits.
from core.config import get_vault_dir  # noqa: F401

_ENV_PATH = Path.home() / ".company-os" / ".env"


def read_env_file(path: Path = _ENV_PATH) -> dict[str, str]:
    """Parse `path` as a .env file and return a dict. Pure parse — does
    NOT mutate os.environ. Callers that want to layer file values under
    os.environ should do so explicitly.

    Rules: KEY=value, # comments, no interpolation. Surrounding single or
    double quotes are stripped.
    """
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        if key:
            out[key] = value
    return out


def load_env(path: Path = _ENV_PATH) -> dict[str, str]:
    """Parse `path` as a .env file and set missing keys into os.environ.

    Returns a dict of keys that were set (for logging/debugging).
    """
    loaded: dict[str, str] = {}
    parsed = read_env_file(path)
    for key, value in parsed.items():
        # Set if missing OR if current value is empty (e.g. Claude Code sets
        # ANTHROPIC_API_KEY="" as a placeholder — the .env value should win)
        if not os.environ.get(key):
            os.environ[key] = value
            loaded[key] = value
    return loaded


class MissingRequiredEnv(RuntimeError):
    """Raised at startup when a required environment variable is missing or
    empty. Callers catch this at the boot path and exit with a clear error
    rather than failing later on the first LLM call."""


def validate_runtime_environment(
    required: tuple[str, ...] = ("ANTHROPIC_API_KEY",),
) -> None:
    """Fail fast if any `required` env var is unset or empty.

    Intended to be called once at the top of a long-running process (webapp
    boot, CLI dispatch, scheduled task). `load_env()` should run first so
    `.env`-sourced values are visible.

    Silent absence of ANTHROPIC_API_KEY currently surfaces as an opaque
    anthropic SDK error on the first `messages.create()` call, which can be
    minutes of wall clock time into a long dispatch. Catching it at boot
    lets the operator correct before any job starts.
    """
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        joined = ", ".join(missing)
        raise MissingRequiredEnv(
            "Required environment variable(s) missing or empty: "
            f"{joined}. Set them in ~/.company-os/.env or the process "
            "environment before starting. See docs/runbook-webapp.md.",
        )
