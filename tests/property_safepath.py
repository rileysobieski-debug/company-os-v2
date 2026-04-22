"""Pre-kernel adversarial harness: SafePath containment.

Week 1 plan item. Intentionally runnable against the Week 2-3 stub so
the test design surfaces before implementation. Expected state:

  - With the stub: all containment tests xfail (SafePath.resolve
    raises NotImplementedError).
  - After Week 2-3: the `strict=True` xfail markers flip to pass,
    and CI flags if any adversarial probe slips through.

The attacks tested here are intentionally nasty. Shallow tests (only
`..`) are not enough; the reviewer demanded adversarial coverage.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from core.safepath import SafePath, SovereignBreach


@pytest.fixture
def tenant_root(tmp_path):
    root = tmp_path / "tenant-uuid"
    root.mkdir()
    (root / "allowed.txt").write_text("ok", encoding="utf-8")
    outside = tmp_path / "sibling"
    outside.mkdir()
    (outside / "secret.txt").write_text("stolen", encoding="utf-8")
    return root


# ---------------------------------------------------------------------------
# Targeted adversarial probes
# ---------------------------------------------------------------------------
_ATTACK_VECTORS = [
    "..",
    "../sibling/secret.txt",
    "../../etc/passwd",
    "/etc/passwd",
    "C:\\Windows\\System32",
    "\\\\?\\C:\\Windows\\System32",
    "subdir/../../sibling/secret.txt",
    "allowed.txt\x00/../sibling/secret.txt",
    "./././../sibling",
    "%2e%2e%2fsibling",
]


@pytest.mark.xfail(
    reason="SafePath.resolve is a Week 2-3 deliverable; stub raises NotImplementedError",
    strict=True,
    raises=NotImplementedError,
)
@pytest.mark.parametrize("vector", _ATTACK_VECTORS)
def test_attack_vectors_raise_breach(tenant_root, vector):
    sp = SafePath(tenant_root)
    with pytest.raises(SovereignBreach):
        sp.resolve(vector)


@pytest.mark.xfail(
    reason="SafePath.resolve is a Week 2-3 deliverable",
    strict=True,
    raises=NotImplementedError,
)
def test_legit_relative_path_resolves_inside_root(tenant_root):
    sp = SafePath(tenant_root)
    resolved = sp.resolve("allowed.txt")
    assert Path(resolved).read_text(encoding="utf-8") == "ok"
    assert Path(resolved).resolve().is_relative_to(tenant_root.resolve())


@pytest.mark.xfail(
    reason="SafePath.resolve is a Week 2-3 deliverable",
    strict=True,
    raises=NotImplementedError,
)
def test_symlink_to_outside_root_blocked(tmp_path, tenant_root):
    outside = tmp_path / "sibling" / "secret.txt"
    link = tenant_root / "evil-link"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlink unsupported on this platform")
    sp = SafePath(tenant_root)
    with pytest.raises(SovereignBreach):
        sp.resolve("evil-link")


# ---------------------------------------------------------------------------
# Property-based: fuzz random byte-ish paths, none should escape
# ---------------------------------------------------------------------------
@pytest.mark.xfail(
    reason="SafePath.resolve is a Week 2-3 deliverable",
    strict=True,
    raises=NotImplementedError,
)
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    st.text(
        alphabet=st.characters(blacklist_categories=("Cs",)),
        min_size=1,
        max_size=80,
    ),
)
def test_fuzz_never_escapes_root(tenant_root, candidate):
    sp = SafePath(tenant_root)
    try:
        resolved = sp.resolve(candidate)
    except (SovereignBreach, ValueError, OSError):
        return
    assert Path(resolved).resolve().is_relative_to(tenant_root.resolve())
