"""Pytest fixtures + sys.path bootstrap for agent-settlement-sim tests.

This conftest registers `agent_settlement_sim` (underscore) as a sys.modules
alias for the `agent-settlement-sim/` package directory (which has a hyphen
and cannot be imported by name normally). This must happen before any test
module is imported.
"""
from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path

# ---- 1. Repo root on sys.path so core.primitives resolves. ----
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# ---- 2. Register agent-settlement-sim/ as agent_settlement_sim ----
# Python cannot import a hyphenated directory by name.  We load the package
# manually and bind it under the underscore alias so test modules can do:
#   from agent_settlement_sim.researcher_loop import ResearcherSim
_SIM_DIR = Path(__file__).resolve().parent.parent  # agent-settlement-sim/


def _register_sim_package() -> None:
    """Register agent_settlement_sim + sub-modules in sys.modules."""
    pkg_name = "agent_settlement_sim"
    if pkg_name in sys.modules:
        return  # already registered

    # Register the top-level package.
    init_path = _SIM_DIR / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        pkg_name,
        init_path,
        submodule_search_locations=[str(_SIM_DIR)],
    )
    pkg = importlib.util.module_from_spec(spec)
    sys.modules[pkg_name] = pkg
    spec.loader.exec_module(pkg)

    # Register sub-modules that tests import explicitly.
    sub_modules = [
        ("researcher_loop", _SIM_DIR / "researcher_loop.py"),
        ("tests.fixtures", _SIM_DIR / "tests" / "fixtures.py"),
    ]
    for sub_name, path in sub_modules:
        full_name = f"{pkg_name}.{sub_name}"
        if full_name in sys.modules:
            continue
        parent_attr = sub_name.rsplit(".", 1)
        sub_spec = importlib.util.spec_from_file_location(full_name, path)
        sub_mod = importlib.util.module_from_spec(sub_spec)
        if "." in sub_name:
            # nested: agent_settlement_sim.tests.fixtures
            # Register the intermediate tests package too (as a plain module).
            intermediate = f"{pkg_name}.tests"
            if intermediate not in sys.modules:
                # Create a namespace package for the tests sub-package.
                import types
                ns = types.ModuleType(intermediate)
                ns.__path__ = [str(_SIM_DIR / "tests")]
                ns.__package__ = intermediate
                sys.modules[intermediate] = ns
                setattr(sys.modules[pkg_name], "tests", ns)
        sys.modules[full_name] = sub_mod
        sub_spec.loader.exec_module(sub_mod)
        # Bind as attribute on parent.
        parent_name = full_name.rsplit(".", 1)[0]
        parent_mod = sys.modules.get(parent_name)
        if parent_mod is not None:
            attr = full_name.rsplit(".", 1)[-1]
            setattr(parent_mod, attr, sub_mod)


_register_sim_package()
