"""
agent_settlement_sim.py -- Module alias shim for agent-settlement-sim/.

Python cannot import a hyphenated directory by name. This shim makes
`import agent_settlement_sim` resolve to the actual package by importing
everything from it via importlib and re-exporting under the underscore name.

Usage: pytest discovers this file as a module when the repo root is on sys.path.
"""
from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path

_SIM_DIR = Path(__file__).resolve().parent / "agent-settlement-sim"


def _bootstrap() -> None:
    pkg_name = "agent_settlement_sim"
    if pkg_name in sys.modules:
        return

    init_path = _SIM_DIR / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        pkg_name,
        init_path,
        submodule_search_locations=[str(_SIM_DIR)],
    )
    pkg = importlib.util.module_from_spec(spec)
    sys.modules[pkg_name] = pkg
    spec.loader.exec_module(pkg)


_bootstrap()

# Re-export the package object as this module.
import sys as _sys
_this = _sys.modules[__name__]
_pkg = _sys.modules["agent_settlement_sim"]
# Merge attributes.
for _attr in dir(_pkg):
    if not _attr.startswith("__"):
        setattr(_this, _attr, getattr(_pkg, _attr))
