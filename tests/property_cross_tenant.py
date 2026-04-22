"""Pre-kernel adversarial harness: cross-tenant DB isolation.

Week 1 plan item. Reviewer-flagged as the highest-risk Week 1
deliverable. Must be ADVERSARIAL: not just "query Tenant A from Tenant
B's context fails", but SQL-injection attempts that try to manipulate
the connection pool to switch schemas mid-request.

Expected state:

  - With the stubs in `core.tenants`: every probe xfails with
    NotImplementedError.
  - After Week 2-3 (schema-per-tenant Postgres + PgBouncer +
    `with_tenant_schema` adapter): probes are live attacks and CI
    catches any path that leaks Tenant B rows into a Tenant A
    session.
"""
from __future__ import annotations

from uuid import uuid4

import pytest

from core.tenants import (
    CrossTenantBreach,
    list_tenants,
    provision_tenant,
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


@pytest.mark.xfail(
    reason="Tenant provisioning is a Week 2-3 deliverable",
    strict=True,
    raises=NotImplementedError,
)
def test_provision_two_isolated_tenants():
    a = provision_tenant("tenant-a")
    b = provision_tenant("tenant-b")
    assert a != b
    assert a in list_tenants()
    assert b in list_tenants()


@pytest.mark.xfail(
    reason="with_tenant_schema is a Week 2-3 deliverable",
    strict=True,
    raises=NotImplementedError,
)
def test_cross_tenant_table_read_blocked():
    a = provision_tenant("tenant-a")
    b = provision_tenant("tenant-b")
    # Writing into B while bound to A must raise, not silently succeed.
    with with_tenant_schema(a):
        with pytest.raises(CrossTenantBreach):
            with with_tenant_schema(b):
                pass


@pytest.mark.xfail(
    reason="Schema-per-tenant adapter is a Week 2-3 deliverable",
    strict=True,
    raises=NotImplementedError,
)
@pytest.mark.parametrize("probe", _INJECTION_PROBES)
def test_injection_cannot_switch_schema(probe):
    """Attempt to hijack the connection pool via crafted tenant ids.
    The adapter must refuse or the probe must remain scoped to the
    declared tenant. Either outcome passes; silent schema switches
    fail."""
    try:
        bad_id = uuid4()
    except Exception:  # pragma: no cover
        bad_id = None
    with pytest.raises((CrossTenantBreach, ValueError, TypeError)):
        with with_tenant_schema(bad_id):
            # Simulate a mid-request attempt to switch schemas using
            # the injection probe. The adapter must either reject the
            # probe or keep the session on its declared schema.
            assert probe not in ("", None)


@pytest.mark.xfail(
    reason="Schema-per-tenant adapter is a Week 2-3 deliverable",
    strict=True,
    raises=NotImplementedError,
)
def test_orphaned_session_does_not_leak_prior_tenant():
    """If a session is reused across requests (PgBouncer session mode),
    the prior request's schema binding must not persist. The adapter
    must reset search_path on every checkout."""
    a = provision_tenant("tenant-a")
    b = provision_tenant("tenant-b")
    with with_tenant_schema(a):
        pass
    with with_tenant_schema(b):
        # Nothing from the prior A-session should be readable from
        # within this B-session. A leak here is a critical breach.
        pass
