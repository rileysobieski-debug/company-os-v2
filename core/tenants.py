"""Tenant provisioning and schema-per-tenant isolation primitives.

Stub for the Week 2-3 Walls layer. Real implementation uses schema-per-tenant
Postgres with PgBouncer session-mode pooling. This module exists now so the
pre-kernel cross-tenant harness (`tests/property_cross_tenant.py`) has an
import target; the harness is expected to xfail against the stub.
"""
from __future__ import annotations

from uuid import UUID


class TenantNotFound(LookupError):
    """Raised when a tenant id does not resolve to a provisioned schema."""


class CrossTenantBreach(Exception):
    """Raised when a query attempts to touch data outside the currently
    active tenant schema. Invariant: the Phase 2 Postgres adapter must
    make this condition unreachable in normal operation, so a raise
    indicates either a test probe or a real attack."""


def list_tenants() -> list[UUID]:
    raise NotImplementedError(
        "list_tenants is a Phase 2 (Week 2-3) deliverable. "
        "Stub intentionally fails so pre-kernel harness tests xfail.",
    )


def provision_tenant(slug: str) -> UUID:
    raise NotImplementedError(
        "provision_tenant is a Phase 2 (Week 2-3) deliverable.",
    )


def with_tenant_schema(tenant_id: UUID):
    """Context manager that binds the DB adapter to `tenant_id`'s schema
    for the duration of the block. Stub; Phase 2 ships the real impl."""
    raise NotImplementedError(
        "with_tenant_schema is a Phase 2 (Week 2-3) deliverable.",
    )
