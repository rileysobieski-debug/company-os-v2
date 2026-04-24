"""Database adapter layer for the Walls (Week 2-3) kernel isolation.

The chassis keeps per-tenant state behind an abstract `DatabaseAdapter`
so the engine is not coupled to any specific storage backend. Phase 2
ships two concrete adapters:

    - SQLiteDevAdapter: one SQLite file per tenant. For local development
      and the CI test suite. Isolation is physical (separate DB files);
      cross-tenant leaks are impossible because one adapter instance
      only ever holds an open connection to the currently-bound tenant.

    - PostgresTenantAdapter: schema-per-tenant on a shared Postgres
      cluster. This is the production target. The class exists now as
      a concrete implementation shell with contract tests; the actual
      psycopg3 wiring is gated on first production tenant (Weeks 9-12
      per v6 plan).

Call sites never construct adapters directly. They go through
`core.tenants.with_tenant_schema(tenant_id)` which resolves the correct
adapter, opens a tenant-bound connection, and unbinds on exit. A call
made outside an active `with_tenant_schema` block raises
`TenantNotBoundError`.
"""
from __future__ import annotations

import sqlite3
import threading
from abc import ABC, abstractmethod
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from uuid import UUID


class AdapterError(RuntimeError):
    """Base class for all adapter failures."""


class TenantNotBoundError(AdapterError):
    """Raised when a DB call happens outside a `with_tenant_schema` block."""


class TenantAlreadyBoundError(AdapterError):
    """Raised when `with_tenant_schema` is entered while a different tenant
    is already bound on the current thread. Cross-tenant nesting is a
    containment violation, not a feature."""


class DatabaseAdapter(ABC):
    """Storage backend contract. Implementations own tenant-scoped
    connections and enforce the `one tenant per connection` invariant."""

    @abstractmethod
    def provision_tenant(self, tenant_id: UUID, tenant_root: Path) -> None:
        """Create the physical storage for a new tenant. Idempotent:
        calling twice with the same `tenant_id` is a no-op."""

    @abstractmethod
    def list_tenant_ids(self) -> list[UUID]:
        """Return every tenant id known to this adapter, in stable order."""

    @abstractmethod
    def bind(self, tenant_id: UUID) -> None:
        """Mark `tenant_id` as the active tenant on the current thread.
        Raises `TenantAlreadyBoundError` if a different tenant is already
        bound. Raises `AdapterError` if the tenant is unknown."""

    @abstractmethod
    def unbind(self) -> None:
        """Release the current-thread binding. Must be safe to call when
        nothing is bound."""

    @abstractmethod
    def current_tenant(self) -> UUID | None:
        """Return the currently-bound tenant id for this thread, or None."""

    @abstractmethod
    def connect(self) -> sqlite3.Connection:
        """Open a new connection scoped to the currently-bound tenant.
        Raises `TenantNotBoundError` when no tenant is bound. Callers
        are expected to close the returned connection; prefer using
        `with_connection` which handles close on context exit."""

    @contextmanager
    def with_connection(self) -> Iterator[sqlite3.Connection]:
        """Yield a short-lived connection for the currently-bound tenant.
        Default implementation is straight open-close; subclasses can
        override to pool."""
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()


class SQLiteDevAdapter(DatabaseAdapter):
    """File-per-tenant SQLite adapter. Each tenant gets its own `.db`
    file under `<tenant_root>/tenant.db`. Cross-tenant queries are
    impossible at the physical file level; no shared connection pool
    touches two tenants in the same process state.

    Thread isolation: tenant binding is stored in `threading.local`, so
    each request thread holds its own current-tenant marker. Concurrent
    requests across tenants do not contaminate each other's bindings.
    """

    _SCHEMA_VERSION_TABLE = """
        CREATE TABLE IF NOT EXISTS schema_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """

    def __init__(self, registry_dir: Path) -> None:
        """`registry_dir` is where the adapter keeps its tenant index.
        Each tenant's DB lives at `<registry_dir>/<uuid>/tenant.db`."""
        self._registry_dir = Path(registry_dir).resolve()
        self._registry_dir.mkdir(parents=True, exist_ok=True)
        self._thread_state = threading.local()
        self._lock = threading.Lock()

    # ---- identity -------------------------------------------------------
    def tenant_db_path(self, tenant_id: UUID) -> Path:
        return self._registry_dir / str(tenant_id) / "tenant.db"

    # ---- DatabaseAdapter API -------------------------------------------
    def provision_tenant(self, tenant_id: UUID, tenant_root: Path) -> None:
        db_path = self.tenant_db_path(tenant_id)
        if db_path.exists():
            return
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute(self._SCHEMA_VERSION_TABLE)
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) VALUES (?, ?)",
                ("schema_version", "0"),
            )
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) VALUES (?, ?)",
                ("tenant_id", str(tenant_id)),
            )
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) VALUES (?, ?)",
                ("tenant_root", str(tenant_root)),
            )
            conn.commit()

    def list_tenant_ids(self) -> list[UUID]:
        ids: list[UUID] = []
        for child in sorted(self._registry_dir.iterdir()):
            if not child.is_dir():
                continue
            if not (child / "tenant.db").exists():
                continue
            try:
                ids.append(UUID(child.name))
            except ValueError:
                continue
        return ids

    def bind(self, tenant_id: UUID) -> None:
        if not self.tenant_db_path(tenant_id).exists():
            raise AdapterError(
                f"tenant {tenant_id} has no provisioned storage; "
                "call provision_tenant first",
            )
        existing = getattr(self._thread_state, "tenant_id", None)
        if existing is not None and existing != tenant_id:
            raise TenantAlreadyBoundError(
                f"thread already bound to tenant {existing}; "
                f"cannot rebind to {tenant_id} without unbind",
            )
        self._thread_state.tenant_id = tenant_id

    def unbind(self) -> None:
        self._thread_state.tenant_id = None

    def current_tenant(self) -> UUID | None:
        return getattr(self._thread_state, "tenant_id", None)

    def connect(self) -> sqlite3.Connection:
        tenant_id = self.current_tenant()
        if tenant_id is None:
            raise TenantNotBoundError(
                "no tenant is bound on this thread; "
                "use `with_tenant_schema(tenant_id)` before opening a connection",
            )
        db_path = self.tenant_db_path(tenant_id)
        if not db_path.exists():
            raise AdapterError(
                f"tenant {tenant_id} bound but storage missing at {db_path}",
            )
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn


class PostgresTenantAdapter(DatabaseAdapter):
    """Schema-per-tenant Postgres adapter. Production target. Scoped as
    an explicit shell until first production tenant; raising
    NotImplementedError is intentional so any accidental usage fails
    loudly instead of silently degrading.

    Real implementation (Weeks 9-12 per v6 plan) will:

    - Use `psycopg[binary] >= 3.2` through a PgBouncer session-mode pool.
    - `provision_tenant` -> `CREATE SCHEMA tenant_<id>`, apply migrations.
    - `bind` -> issue `SET search_path TO tenant_<id>, public` at
      connection checkout.
    - `unbind` -> `RESET search_path` so a returned connection cannot
      leak a prior tenant's schema into the next checkout (this is the
      attack vector that `tests/property_cross_tenant.py` probes with
      injection-shaped tenant ids).
    - Parameterize every tenant id bind so SQL injection at the session
      layer is physically impossible.
    """

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        raise NotImplementedError(
            "PostgresTenantAdapter is not wired yet. "
            "Use SQLiteDevAdapter until production tenant onboarding. "
            "Plan: v6 Weeks 9-12. dsn received but ignored.",
        )

    def provision_tenant(self, tenant_id: UUID, tenant_root: Path) -> None:  # pragma: no cover
        raise NotImplementedError

    def list_tenant_ids(self) -> list[UUID]:  # pragma: no cover
        raise NotImplementedError

    def bind(self, tenant_id: UUID) -> None:  # pragma: no cover
        raise NotImplementedError

    def unbind(self) -> None:  # pragma: no cover
        raise NotImplementedError

    def current_tenant(self) -> UUID | None:  # pragma: no cover
        raise NotImplementedError

    def connect(self) -> sqlite3.Connection:  # pragma: no cover
        raise NotImplementedError
