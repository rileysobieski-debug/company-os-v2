"""Tenant provisioning + with_tenant_schema containment tests.

Covers `core.tenants` against `SQLiteDevAdapter`. The cross-tenant
isolation probes (including the 7 SQL-injection-shaped tenant ids the
reviewer round demanded) live in `tests/test_cross_tenant.py` which
graduated from the Week 1 xfail-strict harness once the real
provisioning landed.
"""
from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

import pytest

from core.db_adapter import SQLiteDevAdapter
from core.tenants import (
    CrossTenantBreach,
    Tenant,
    TenantNotFound,
    TenantRegistry,
    current_tenant,
    get_tenant,
    list_tenants,
    provision_tenant,
    tenant_root,
    use_registry,
    with_tenant_schema,
)


@pytest.fixture
def registry(tmp_path: Path) -> TenantRegistry:
    adapter = SQLiteDevAdapter(tmp_path)
    return TenantRegistry(tmp_path, adapter)


@pytest.fixture
def swapped(registry):
    with use_registry(registry) as r:
        yield r


# ---------------------------------------------------------------------------
# provision / lookup
# ---------------------------------------------------------------------------
def test_provision_returns_uuid(swapped) -> None:
    tid = provision_tenant("acme")
    assert isinstance(tid, UUID)


def test_provision_is_slug_idempotent(swapped) -> None:
    a = provision_tenant("acme")
    b = provision_tenant("acme")
    assert a == b


def test_provision_distinct_slugs_get_distinct_ids(swapped) -> None:
    a = provision_tenant("alpha")
    b = provision_tenant("bravo")
    assert a != b


def test_provision_rejects_empty_slug(swapped) -> None:
    with pytest.raises(ValueError):
        provision_tenant("")


def test_provision_rejects_whitespace_slug(swapped) -> None:
    with pytest.raises(ValueError):
        provision_tenant("   ")


def test_list_tenants_returns_provisioned_ids(swapped) -> None:
    a = provision_tenant("alpha")
    b = provision_tenant("bravo")
    assert set(list_tenants()) == {a, b}


def test_get_tenant_returns_metadata(swapped) -> None:
    tid = provision_tenant("acme")
    tenant = get_tenant(tid)
    assert tenant is not None
    assert tenant.slug == "acme"
    assert tenant.tenant_id == tid


def test_get_tenant_unknown_is_none(swapped) -> None:
    assert get_tenant(uuid4()) is None


def test_tenant_root_for_unknown_raises(swapped) -> None:
    with pytest.raises(TenantNotFound):
        tenant_root(uuid4())


def test_tenant_root_is_inside_registry(swapped, tmp_path: Path) -> None:
    tid = provision_tenant("acme")
    root = tenant_root(tid)
    assert root.is_relative_to(tmp_path.resolve())


# ---------------------------------------------------------------------------
# with_tenant_schema
# ---------------------------------------------------------------------------
def test_with_tenant_schema_binds_on_enter(swapped) -> None:
    tid = provision_tenant("acme")
    assert current_tenant() is None
    with with_tenant_schema(tid):
        assert current_tenant() == tid
    assert current_tenant() is None


def test_with_tenant_schema_unbinds_on_exit_even_on_error(swapped) -> None:
    tid = provision_tenant("acme")
    with pytest.raises(RuntimeError):
        with with_tenant_schema(tid):
            raise RuntimeError("kaboom")
    assert current_tenant() is None


def test_with_tenant_schema_unknown_tenant_raises(swapped) -> None:
    with pytest.raises(TenantNotFound):
        with with_tenant_schema(uuid4()):
            pass


def test_with_tenant_schema_rejects_non_uuid_input(swapped) -> None:
    with pytest.raises(TypeError):
        with with_tenant_schema("not-a-uuid"):  # type: ignore[arg-type]
            pass


def test_cross_tenant_nesting_raises(swapped) -> None:
    a = provision_tenant("alpha")
    b = provision_tenant("bravo")
    with with_tenant_schema(a):
        with pytest.raises(CrossTenantBreach):
            with with_tenant_schema(b):
                pass


def test_same_tenant_nesting_is_reentrant(swapped) -> None:
    tid = provision_tenant("acme")
    with with_tenant_schema(tid):
        with with_tenant_schema(tid):
            assert current_tenant() == tid
        # Outer scope still bound after inner exits.
        assert current_tenant() == tid
    assert current_tenant() is None


# ---------------------------------------------------------------------------
# Registry persistence
# ---------------------------------------------------------------------------
def test_registry_survives_process_restart(tmp_path: Path) -> None:
    adapter1 = SQLiteDevAdapter(tmp_path)
    reg1 = TenantRegistry(tmp_path, adapter1)
    tid = reg1.provision("acme")
    # New adapter + registry instance (simulating a process restart)
    adapter2 = SQLiteDevAdapter(tmp_path)
    reg2 = TenantRegistry(tmp_path, adapter2)
    assert reg2.get(tid) is not None
    assert reg2.get(tid).slug == "acme"


def test_registry_atomic_write_recovers_from_partial(tmp_path: Path) -> None:
    """A stray .json.tmp from a crashed write must not confuse a fresh
    registry load. This is the Gemini `scenarios.jsonl` concern applied
    to the tenant index: atomic replace keeps the final file consistent,
    and an orphan tmp is ignored."""
    adapter = SQLiteDevAdapter(tmp_path)
    (tmp_path / "registry.json.tmp").write_text("{corrupted", encoding="utf-8")
    reg = TenantRegistry(tmp_path, adapter)
    tid = reg.provision("acme")
    assert tid == reg.get_by_slug("acme").tenant_id


def test_registry_load_survives_malformed_index(tmp_path: Path) -> None:
    adapter = SQLiteDevAdapter(tmp_path)
    (tmp_path / "registry.json").write_text("not json at all", encoding="utf-8")
    reg = TenantRegistry(tmp_path, adapter)
    # Malformed index treated as empty; fresh provisioning recovers.
    tid = reg.provision("acme")
    assert reg.get(tid).slug == "acme"


def test_tenant_is_frozen_dataclass() -> None:
    tenant = Tenant(
        tenant_id=uuid4(),
        slug="acme",
        created_at="2026-04-24T00:00:00+00:00",
        root=Path("/tmp/t"),
    )
    with pytest.raises(Exception):
        tenant.slug = "mutated"  # type: ignore[misc]
