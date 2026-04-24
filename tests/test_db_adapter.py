"""DatabaseAdapter + SQLiteDevAdapter contract tests.

The adapter contract is load-bearing for Walls-layer containment; every
behavior asserted here is a cross-tenant isolation invariant. Prefer
adding a new assertion here over burying it in a downstream module test,
because this file is where a future PostgresTenantAdapter will reuse
the same test matrix.
"""
from __future__ import annotations

import threading
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from core.db_adapter import (
    AdapterError,
    DatabaseAdapter,
    PostgresTenantAdapter,
    SQLiteDevAdapter,
    TenantAlreadyBoundError,
    TenantNotBoundError,
)


@pytest.fixture
def adapter(tmp_path: Path) -> SQLiteDevAdapter:
    return SQLiteDevAdapter(tmp_path / "registry")


# ---------------------------------------------------------------------------
# Provisioning
# ---------------------------------------------------------------------------
def test_provision_creates_tenant_db_file(adapter: SQLiteDevAdapter, tmp_path: Path) -> None:
    tenant_id = uuid4()
    tenant_root = tmp_path / "root-a"
    tenant_root.mkdir()
    adapter.provision_tenant(tenant_id, tenant_root)
    assert adapter.tenant_db_path(tenant_id).exists()


def test_provision_is_idempotent(adapter: SQLiteDevAdapter, tmp_path: Path) -> None:
    tenant_id = uuid4()
    tenant_root = tmp_path / "root-a"
    tenant_root.mkdir()
    adapter.provision_tenant(tenant_id, tenant_root)
    adapter.provision_tenant(tenant_id, tenant_root)
    assert adapter.tenant_db_path(tenant_id).exists()


def test_provision_writes_schema_meta(adapter: SQLiteDevAdapter, tmp_path: Path) -> None:
    import sqlite3
    tenant_id = uuid4()
    tenant_root = tmp_path / "root"
    tenant_root.mkdir()
    adapter.provision_tenant(tenant_id, tenant_root)
    with sqlite3.connect(adapter.tenant_db_path(tenant_id)) as conn:
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'tenant_id'",
        ).fetchone()
    assert row is not None and row[0] == str(tenant_id)


# ---------------------------------------------------------------------------
# Binding + containment
# ---------------------------------------------------------------------------
def test_bind_requires_provisioned_tenant(adapter: SQLiteDevAdapter) -> None:
    with pytest.raises(AdapterError):
        adapter.bind(uuid4())


def test_bind_then_unbind(adapter: SQLiteDevAdapter, tmp_path: Path) -> None:
    tenant_id = uuid4()
    adapter.provision_tenant(tenant_id, tmp_path / "r")
    assert adapter.current_tenant() is None
    adapter.bind(tenant_id)
    assert adapter.current_tenant() == tenant_id
    adapter.unbind()
    assert adapter.current_tenant() is None


def test_cross_tenant_bind_raises(adapter: SQLiteDevAdapter, tmp_path: Path) -> None:
    a = uuid4()
    b = uuid4()
    adapter.provision_tenant(a, tmp_path / "a")
    adapter.provision_tenant(b, tmp_path / "b")
    adapter.bind(a)
    with pytest.raises(TenantAlreadyBoundError):
        adapter.bind(b)
    adapter.unbind()


def test_rebind_same_tenant_is_noop(adapter: SQLiteDevAdapter, tmp_path: Path) -> None:
    t = uuid4()
    adapter.provision_tenant(t, tmp_path / "t")
    adapter.bind(t)
    adapter.bind(t)
    assert adapter.current_tenant() == t
    adapter.unbind()


def test_connect_without_binding_raises(adapter: SQLiteDevAdapter) -> None:
    with pytest.raises(TenantNotBoundError):
        adapter.connect()


def test_connect_after_binding_returns_live_connection(adapter: SQLiteDevAdapter, tmp_path: Path) -> None:
    t = uuid4()
    adapter.provision_tenant(t, tmp_path / "t")
    adapter.bind(t)
    try:
        with adapter.with_connection() as conn:
            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key = 'tenant_id'",
            ).fetchone()
            assert row[0] == str(t)
    finally:
        adapter.unbind()


# ---------------------------------------------------------------------------
# Thread isolation
# ---------------------------------------------------------------------------
def test_thread_local_binding_does_not_leak(adapter: SQLiteDevAdapter, tmp_path: Path) -> None:
    t = uuid4()
    adapter.provision_tenant(t, tmp_path / "t")
    results: list[UUID | None] = []

    def worker() -> None:
        # A fresh thread starts with no binding, regardless of what the
        # main thread has bound. This is the invariant that keeps a
        # pooled worker from inheriting the previous request's tenant.
        results.append(adapter.current_tenant())

    adapter.bind(t)
    try:
        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()
    finally:
        adapter.unbind()

    assert results == [None]


def test_concurrent_threads_bind_different_tenants(adapter: SQLiteDevAdapter, tmp_path: Path) -> None:
    a = uuid4()
    b = uuid4()
    adapter.provision_tenant(a, tmp_path / "a")
    adapter.provision_tenant(b, tmp_path / "b")
    seen: dict[str, UUID | None] = {}
    barrier = threading.Barrier(2)

    def worker(label: str, tenant_id: UUID) -> None:
        adapter.bind(tenant_id)
        try:
            barrier.wait(timeout=5)
            seen[label] = adapter.current_tenant()
        finally:
            adapter.unbind()

    threads = [
        threading.Thread(target=worker, args=("a", a)),
        threading.Thread(target=worker, args=("b", b)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert seen == {"a": a, "b": b}


# ---------------------------------------------------------------------------
# list_tenant_ids
# ---------------------------------------------------------------------------
def test_list_tenant_ids_stable_order(adapter: SQLiteDevAdapter, tmp_path: Path) -> None:
    ids = [uuid4() for _ in range(5)]
    for tid in ids:
        adapter.provision_tenant(tid, tmp_path / str(tid))
    listed = adapter.list_tenant_ids()
    assert sorted(listed, key=str) == listed
    assert set(listed) == set(ids)


def test_list_tenant_ids_skips_non_uuid_dirs(adapter: SQLiteDevAdapter, tmp_path: Path) -> None:
    # A stray directory inside the registry (e.g. a human-created
    # README/ folder) must not be interpreted as a tenant.
    (adapter._registry_dir / "not-a-uuid").mkdir()
    tid = uuid4()
    adapter.provision_tenant(tid, tmp_path / "r")
    assert adapter.list_tenant_ids() == [tid]


# ---------------------------------------------------------------------------
# PostgresTenantAdapter shell
# ---------------------------------------------------------------------------
def test_postgres_adapter_constructor_is_explicit_notimpl() -> None:
    # The shell exists to guarantee the public API compiles; live wiring
    # is Weeks 9-12. Any accidental instantiation must fail loudly, not
    # silently degrade to a non-isolated backend.
    with pytest.raises(NotImplementedError):
        PostgresTenantAdapter(dsn="postgresql://placeholder/company_os")


# ---------------------------------------------------------------------------
# Abstract base sanity
# ---------------------------------------------------------------------------
def test_database_adapter_is_abstract() -> None:
    with pytest.raises(TypeError):
        DatabaseAdapter()  # type: ignore[abstract]
