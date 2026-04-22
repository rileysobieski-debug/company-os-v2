"""SafePath: chroot-style filesystem containment for per-tenant reads.

Stub for the Week 2-3 Walls layer. Real implementation lands with the
kernel-isolation PR. This module exists now so the pre-kernel test
harness (`tests/property_safepath.py`) has a concrete import target
to exercise; the harness is expected to xfail against the stub.

The contract, once implemented:

    SafePath(tenant_root).resolve(relative_path)

    - Rejects absolute paths.
    - Rejects paths whose resolved form escapes the tenant root.
    - Rejects paths containing null bytes, null-equivalent Unicode
      characters, or Windows short-name variants that collapse to an
      outside-root target.
    - Symlinks are followed; the resolved target must still live
      inside the root or `SovereignBreach` is raised.
"""
from __future__ import annotations

from pathlib import Path


class SovereignBreach(Exception):
    """Raised when a resolved path escapes its tenant root. A breach
    kills the request and triggers an alert in the retrolog layer."""


class SafePath:
    """Stub. Phase 2 (Week 2-3) replaces this with the hardened
    implementation. Until then `resolve()` raises NotImplementedError
    so the pre-kernel test harness fails loudly."""

    def __init__(self, tenant_root: str | Path) -> None:
        self._root = Path(tenant_root)

    @property
    def root(self) -> Path:
        return self._root

    def resolve(self, relative: str | Path) -> Path:
        raise NotImplementedError(
            "SafePath.resolve is a Phase 2 (Week 2-3) deliverable. "
            "Stub intentionally fails so pre-kernel harness tests xfail.",
        )
