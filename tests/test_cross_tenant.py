"""Cross-tenant DB isolation tests: adversarial probes.

Graduated from the Week 1 pre-kernel harness (xfail-strict) now that
the real `core.tenants.provision_tenant` + `with_tenant_schema` land.
The reviewer-flagged invariant: cross-tenant data leakage must be
impossible at the physical storage layer, not merely at the query
layer. Every probe here is either a SQL-shaped tenant id (which the
adapter must refuse) or a nested-binding attempt (which must raise).

When the PostgresTenantAdapter ships, this suite re-runs against it
via a parametrized fixture; the injection probes become real
`SET search_path` hijack attempts then. For now, SQLiteDevAdapter is
the concrete backend; the thread-local binding + file-per-tenant
physical layout make cross-tenant switching unreachable in normal
operation.
"""
from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

import pytest

from core.db_adapter import SQLiteDevAdapter
from core.tenants import (
    CrossTenantBreach,
    TenantNotFound,
    TenantRegistry,
    list_tenants,
    provision_tenant,
    use_registry,
    with_tenant_schema,
)


_INJECTION_PROBES = [
    "'; SET search_path TO tenant_b; --",
    "tenant_a'; DROP SCHEMA tenant_b CASCADE; --",
    "tenant_a' UNION ALL SELECT * FROM tenant_b.decisions --",
    "tenant_a\"; SET search_path = tenant_b, public; --",
    "tenant_a/**/UNION/**/SELECT",
    "\x00tenant_b",
    "tenant_a\x0d\x0aSET search_path TO tenant_b",
]


@pytest.fixture
def registry(tmp_path: Path) -> TenantRegistry:
    return TenantRegistry(tmp_path, SQLiteDevAdapter(tmp_path))


@pytest.fixture
def swapped(registry):
    with use_registry(registry) as r:
        yield r


# ---------------------------------------------------------------------------
# Core isolation
# ---------------------------------------------------------------------------
def test_provision_two_isolated_tenants(swapped) -> None:
    a = provision_tenant("tenant-a")
    b = provision_tenant("tenant-b")
    assert a != b
    assert a in list_tenants()
    assert b in list_tenants()


def test_cross_tenant_table_read_blocked(swapped) -> None:
    """Nested `with_tenant_schema` with a different tenant must raise
    CrossTenantBreach. This is the v6 Walls invariant: at no instant
    does a thread hold two tenant bindings at once."""
    a = provision_tenant("tenant-a")
    b = provision_tenant("tenant-b")
    with with_tenant_schema(a):
        with pytest.raises(CrossTenantBreach):
            with with_tenant_schema(b):
                pass


def test_tenant_db_files_are_separate(swapped, tmp_path: Path) -> None:
    """Physical isolation: two tenants' storage live in different
    files. A read in one cannot see rows from the other by construction."""
    a = provision_tenant("tenant-a")
    b = provision_tenant("tenant-b")
    with with_tenant_schema(a):
        with swapped.adapter.with_connection() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS probe (v TEXT)",
            )
            conn.execute("INSERT INTO probe VALUES (?)", ("only-in-a",))
            conn.commit()
    with with_tenant_schema(b):
        with swapped.adapter.with_connection() as conn:
            # Table `probe` does not exist in b's DB at all.
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='probe'",
            ).fetchone()
            assert row is None


# ---------------------------------------------------------------------------
# Injection-shaped tenant ids
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("probe", _INJECTION_PROBES)
def test_injection_cannot_switch_schema(swapped, probe: str) -> None:
    """UUID type check refuses string-shaped injection probes at the
    public API. An attacker cannot even enter `with_tenant_schema`
    with a crafted tenant id because the type check fires first."""
    with pytest.raises((TypeError, CrossTenantBreach, TenantNotFound, ValueError)):
        with with_tenant_schema(probe):  # type: ignore[arg-type]
            assert probe not in ("", None)


def test_unknown_uuid_rejected(swapped) -> None:
    """A syntactically valid but unprovisioned UUID must not silently
    create a tenant or return an empty context. It must raise."""
    bogus = uuid4()
    with pytest.raises(TenantNotFound):
        with with_tenant_schema(bogus):
            pass


# ---------------------------------------------------------------------------
# Thread-reuse leak check
# ---------------------------------------------------------------------------
def test_orphaned_session_does_not_leak_prior_tenant(swapped) -> None:
    """If a worker thread is reused across requests, the prior request's
    binding must not persist. `with_tenant_schema` unbinds on exit, so
    the next with-block on the same thread starts cleanly."""
    import threading
    a = provision_tenant("tenant-a")
    b = provision_tenant("tenant-b")
    observations: list[UUID | None] = []

    def worker():
        with with_tenant_schema(a):
            observations.append(swapped.adapter.current_tenant())
        observations.append(swapped.adapter.current_tenant())
        with with_tenant_schema(b):
            observations.append(swapped.adapter.current_tenant())
        observations.append(swapped.adapter.current_tenant())

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert observations == [a, None, b, None]
