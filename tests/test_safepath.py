"""Adversarial test suite: SafePath containment (Walls layer).

Started Week 1 as a pre-kernel harness (xfail-strict against the stub).
Week 2 Day 1 landed the real SafePath; xfail markers have been removed
and this file is now the live regression suite for tenant path
containment. Every probe here was chosen by the v6 reviewer round as a
known attack vector; keep them even if they feel redundant.
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


@pytest.mark.parametrize("vector", _ATTACK_VECTORS)
def test_attack_vectors_raise_breach(tenant_root, vector):
    sp = SafePath(tenant_root)
    with pytest.raises(SovereignBreach):
        sp.resolve(vector)


def test_legit_relative_path_resolves_inside_root(tenant_root):
    sp = SafePath(tenant_root)
    resolved = sp.resolve("allowed.txt")
    assert Path(resolved).read_text(encoding="utf-8") == "ok"
    assert Path(resolved).resolve().is_relative_to(tenant_root.resolve())


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


# ---------------------------------------------------------------------------
# Constructor-time invariants
# ---------------------------------------------------------------------------
def test_constructor_rejects_nonexistent_root(tmp_path):
    with pytest.raises(SovereignBreach):
        SafePath(tmp_path / "does-not-exist")


def test_root_property_returns_resolved_absolute_path(tenant_root):
    sp = SafePath(tenant_root)
    assert sp.root == tenant_root.resolve()
    assert sp.root.is_absolute()


def test_resolve_accepts_pathlib_input(tenant_root):
    sp = SafePath(tenant_root)
    resolved = sp.resolve(Path("allowed.txt"))
    assert resolved == (tenant_root / "allowed.txt").resolve()


def test_resolve_rejects_null_byte_in_pathlib_input(tenant_root):
    sp = SafePath(tenant_root)
    with pytest.raises(SovereignBreach):
        sp.resolve("allowed.txt\x00")


def test_resolve_allows_nested_subdir(tenant_root):
    nested = tenant_root / "subdir" / "nested"
    nested.mkdir(parents=True)
    (nested / "note.md").write_text("nested", encoding="utf-8")
    sp = SafePath(tenant_root)
    resolved = sp.resolve("subdir/nested/note.md")
    assert resolved.read_text(encoding="utf-8") == "nested"
