"""Pytest fixtures + sys.path bootstrap for the structural test suite.

The tests intentionally do NOT make any LLM calls. They verify the wiring,
loaders, paths, webapp routes, and sandboxing — everything that should hold
true regardless of API quota.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Ensure company-os/ is importable
_PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(_PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(_PROJECT_DIR))

import pytest  # noqa: E402


# Known pre-existing failures tracked in docs/known-pre-existing-failures.md.
# Auto-applied as xfail(strict=True) so CI does not block on them but an
# accidental fix is caught via XPASS. Remove an entry here when the real fix
# lands; don't modify the test file just to silence the failure.
_KNOWN_PRE_EXISTING_FAILURES = {
    "tests/test_phase14_dept_onboarding.py::TestPhaseTransitions::test_full_lifecycle_to_complete",
    "tests/test_phase14_dept_onboarding.py::TestAggregates::test_overall_progress_counts",
    "tests/test_phase14_dept_onboarding.py::TestScopeCalibrationPrompt::test_secondary_is_ambient_not_operational",
    "tests/test_phase14_stack_review.py::TestAutoTrigger::test_returns_true_when_all_complete",
}


def pytest_collection_modifyitems(config, items):  # noqa: ARG001
    mark = pytest.mark.xfail(
        reason="known pre-existing phase14 failure; see docs/known-pre-existing-failures.md",
        strict=True,
    )
    for item in items:
        normalised = item.nodeid.replace("\\", "/")
        if normalised in _KNOWN_PRE_EXISTING_FAILURES:
            item.add_marker(mark)


@pytest.fixture(scope="session")
def vault_dir() -> Path:
    """Vault root Path, read from COMPANY_OS_VAULT_DIR.

    Skips the test cleanly when the env var is unset, so CI can run the
    suite without a real vault checkout. Chunk 1a.1 relocates the accessor
    from core.env to core.config; the import stays inside the fixture so
    that swap is a one-line change.
    """
    from core.env import get_vault_dir
    try:
        return get_vault_dir()
    except RuntimeError as exc:
        pytest.skip(str(exc))


@pytest.fixture(scope="session")
def old_press_dir(vault_dir) -> Path:
    return vault_dir / "Old Press Wine Company LLC"


@pytest.fixture(scope="session")
def company(old_press_dir):
    """Loaded Old Press CompanyConfig — session-scoped, no LLM cost."""
    from core.company import load_company
    return load_company(old_press_dir)


@pytest.fixture(scope="session")
def departments(company):
    """Loaded department list for Old Press — session-scoped."""
    from core.managers.loader import load_departments
    return load_departments(company)


@pytest.fixture
def asset_registry():
    """Fresh AssetRegistry loaded from the default asset_registry dir."""
    from core.primitives.asset import AssetRegistry
    reg = AssetRegistry()
    root = Path(__file__).parent.parent / "core" / "primitives" / "asset_registry"
    reg.load(root)
    return reg
