"""Tenant provisioning and schema-per-tenant isolation primitives (Walls).

Landed Week 2 as the second half of the Walls layer after `SafePath`.
Replaces the Week 1 NotImplementedError stubs. Backed by
`SQLiteDevAdapter` for local dev and CI; `PostgresTenantAdapter` is the
production target and will slot in behind the same public surface when
it lands (v6 Weeks 9-12).

Public surface:

    provision_tenant(slug) -> UUID
        Atomically register a new tenant, allocate its filesystem root
        under `<vault>/_tenants/<uuid>/`, and call the adapter's
        `provision_tenant` to create the physical storage. Idempotent on
        slug; the same slug twice returns the same UUID. Raises
        `TenantSlugConflict` if another slug hashes to the same UUID
        (should not happen with uuid4).

    list_tenants() -> list[UUID]
        Every tenant known to the adapter, stable ordering.

    with_tenant_schema(tenant_id) -> context manager
        Bind the current thread to `tenant_id` for the duration of the
        block. On exit the binding is released so a pooled worker
        thread is always clean for its next task. Nested blocks with
        DIFFERENT tenant ids raise `CrossTenantBreach` because
        cross-tenant nesting is never a valid call pattern. Nested with
        the SAME id is allowed (reentrant).

    get_tenant(tenant_id) -> Tenant | None
        Metadata lookup. None when unknown.

    current_tenant() -> UUID | None
        The thread-local binding, or None.

    tenant_root(tenant_id) -> Path
        Returns the on-disk root for the tenant. Callers passing tenant-
        supplied paths should combine this with `SafePath` for
        containment.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator
from uuid import UUID, uuid4

from core.db_adapter import (
    AdapterError,
    DatabaseAdapter,
    SQLiteDevAdapter,
    TenantAlreadyBoundError,
    TenantNotBoundError,
)


class TenantNotFound(LookupError):
    """Raised when a tenant id does not resolve to a provisioned schema."""


class TenantSlugConflict(ValueError):
    """Raised on an attempt to register two different tenants under
    the same slug."""


class CrossTenantBreach(RuntimeError):
    """Raised when a thread attempts to bind a different tenant while
    already bound to one. The v6 Walls invariant: one tenant per thread
    at any instant; switching tenants requires exiting the outer block
    first."""


@dataclass(frozen=True)
class Tenant:
    tenant_id: UUID
    slug: str
    created_at: str  # ISO-8601 UTC
    root: Path


# Module-level singleton registry. Tests can swap `_registry` via the
# `use_registry` context manager for isolation.
_registry_lock = threading.Lock()
_registry: "TenantRegistry | None" = None


class TenantRegistry:
    """Slug -> Tenant index with a backing `DatabaseAdapter`.

    The registry itself is lightweight metadata stored as a JSON sidecar
    at `<registry_root>/registry.json`. The adapter owns the physical
    per-tenant storage; the registry owns the slug/UUID/created_at map.
    """

    def __init__(self, registry_root: Path, adapter: DatabaseAdapter) -> None:
        self._root = Path(registry_root).resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        self._adapter = adapter
        self._index_path = self._root / "registry.json"
        self._lock = threading.Lock()
        self._index: dict[str, dict[str, str]] = self._load_index()

    # ---- index persistence ---------------------------------------------
    def _load_index(self) -> dict[str, dict[str, str]]:
        import json
        if not self._index_path.exists():
            return {}
        try:
            return json.loads(self._index_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def _write_index(self) -> None:
        import json
        tmp = self._index_path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(self._index, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        tmp.replace(self._index_path)

    # ---- operations -----------------------------------------------------
    def provision(self, slug: str) -> UUID:
        if not slug or not slug.strip():
            raise ValueError("tenant slug must be non-empty")
        slug = slug.strip()
        with self._lock:
            existing = self._index.get(slug)
            if existing is not None:
                return UUID(existing["tenant_id"])
            tenant_id = uuid4()
            tenant_root = self._root / str(tenant_id)
            tenant_root.mkdir(parents=True, exist_ok=True)
            self._adapter.provision_tenant(tenant_id, tenant_root)
            created = datetime.now(timezone.utc).isoformat()
            self._index[slug] = {
                "tenant_id": str(tenant_id),
                "created_at": created,
                "root": str(tenant_root),
            }
            self._write_index()
            return tenant_id

    def get_by_slug(self, slug: str) -> Tenant | None:
        rec = self._index.get(slug)
        if rec is None:
            return None
        return Tenant(
            tenant_id=UUID(rec["tenant_id"]),
            slug=slug,
            created_at=rec["created_at"],
            root=Path(rec["root"]),
        )

    def get(self, tenant_id: UUID) -> Tenant | None:
        for slug, rec in self._index.items():
            if rec["tenant_id"] == str(tenant_id):
                return Tenant(
                    tenant_id=tenant_id,
                    slug=slug,
                    created_at=rec["created_at"],
                    root=Path(rec["root"]),
                )
        return None

    def list_ids(self) -> list[UUID]:
        return sorted(
            (UUID(rec["tenant_id"]) for rec in self._index.values()),
            key=str,
        )

    @property
    def adapter(self) -> DatabaseAdapter:
        return self._adapter


def get_default_registry() -> TenantRegistry:
    """Resolve the process-wide registry. Lazy-initialized from the
    vault dir so tests that never touch tenants do not pay the setup
    cost."""
    global _registry
    with _registry_lock:
        if _registry is None:
            from core.config import get_vault_dir
            vault = get_vault_dir()
            registry_root = vault / "_tenants"
            adapter = SQLiteDevAdapter(registry_root)
            _registry = TenantRegistry(registry_root, adapter)
        return _registry


@contextmanager
def use_registry(registry: TenantRegistry) -> Iterator[TenantRegistry]:
    """Swap the process-wide registry for the duration of a block. For
    tests and worker-pool scenarios where a per-test registry is
    required. Restores the prior binding on exit."""
    global _registry
    with _registry_lock:
        prior = _registry
        _registry = registry
    try:
        yield registry
    finally:
        with _registry_lock:
            _registry = prior


# ---- Public API ----------------------------------------------------------
def provision_tenant(slug: str) -> UUID:
    """Create or look up a tenant by slug. Idempotent."""
    return get_default_registry().provision(slug)


def list_tenants() -> list[UUID]:
    return get_default_registry().list_ids()


def get_tenant(tenant_id: UUID) -> Tenant | None:
    return get_default_registry().get(tenant_id)


def tenant_root(tenant_id: UUID) -> Path:
    tenant = get_tenant(tenant_id)
    if tenant is None:
        raise TenantNotFound(f"unknown tenant: {tenant_id}")
    return tenant.root


def current_tenant() -> UUID | None:
    return get_default_registry().adapter.current_tenant()


@contextmanager
def with_tenant_schema(tenant_id: UUID) -> Iterator[UUID]:
    """Bind `tenant_id` to the current thread for the duration of the
    block. Reentrant with the same id; raises `CrossTenantBreach` on
    nested binding of a different id."""
    if not isinstance(tenant_id, UUID):
        raise TypeError(
            f"with_tenant_schema requires a UUID, got {type(tenant_id).__name__}",
        )
    registry = get_default_registry()
    if registry.get(tenant_id) is None:
        raise TenantNotFound(f"unknown tenant: {tenant_id}")
    adapter = registry.adapter
    prior = adapter.current_tenant()
    reentrant = prior == tenant_id
    if not reentrant:
        try:
            adapter.bind(tenant_id)
        except TenantAlreadyBoundError as exc:
            raise CrossTenantBreach(str(exc)) from exc
    try:
        yield tenant_id
    finally:
        if not reentrant:
            adapter.unbind()
