"""Migration runner tests.

Covers `core.migrations.runner`: discovery, gap detection, idempotent
apply, rollback on error, and the happy-path 001_initial_schema.sql
outcome shape.
"""
from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from core.db_adapter import SQLiteDevAdapter, TenantNotBoundError
from core.migrations.runner import (
    Migration,
    MigrationError,
    apply_migration,
    discover_migrations,
    get_current_version,
    migrate,
)


@pytest.fixture
def adapter(tmp_path: Path) -> SQLiteDevAdapter:
    return SQLiteDevAdapter(tmp_path)


@pytest.fixture
def bound_tenant(adapter: SQLiteDevAdapter, tmp_path: Path):
    tid = uuid4()
    tenant_root = tmp_path / "t"
    tenant_root.mkdir()
    adapter.provision_tenant(tid, tenant_root)
    adapter.bind(tid)
    yield tid
    adapter.unbind()


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------
def test_discover_returns_sorted_migrations() -> None:
    migrations = discover_migrations()
    assert len(migrations) >= 1
    assert [m.version for m in migrations] == sorted(m.version for m in migrations)


def test_discover_starts_at_version_1() -> None:
    migrations = discover_migrations()
    assert migrations[0].version == 1


def test_discover_ignores_non_sql_files(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# not a migration", encoding="utf-8")
    (tmp_path / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "001_schema.sql").write_text("SELECT 1;", encoding="utf-8")
    migrations = discover_migrations(tmp_path)
    assert len(migrations) == 1
    assert migrations[0].version == 1


def test_discover_ignores_malformed_filenames(tmp_path: Path) -> None:
    (tmp_path / "migration.sql").write_text("SELECT 1;", encoding="utf-8")
    (tmp_path / "1_short.sql").write_text("SELECT 1;", encoding="utf-8")
    (tmp_path / "0001_overpadded.sql").write_text("SELECT 1;", encoding="utf-8")
    (tmp_path / "001_good.sql").write_text("SELECT 1;", encoding="utf-8")
    migrations = discover_migrations(tmp_path)
    assert [m.name for m in migrations] == ["good"]


def test_discover_gap_detection(tmp_path: Path) -> None:
    (tmp_path / "001_a.sql").write_text("SELECT 1;", encoding="utf-8")
    (tmp_path / "003_c.sql").write_text("SELECT 1;", encoding="utf-8")
    with pytest.raises(MigrationError):
        discover_migrations(tmp_path)


def test_discover_empty_dir_returns_empty(tmp_path: Path) -> None:
    assert discover_migrations(tmp_path) == []


# ---------------------------------------------------------------------------
# migrate()
# ---------------------------------------------------------------------------
def test_migrate_requires_bound_tenant(adapter: SQLiteDevAdapter) -> None:
    with pytest.raises(TenantNotBoundError):
        migrate(adapter)


def test_migrate_applies_all_migrations(adapter: SQLiteDevAdapter, bound_tenant) -> None:
    applied = migrate(adapter)
    assert applied, "expected at least one migration applied"
    # Idempotent: second call applies none.
    assert migrate(adapter) == []


def test_migrate_advances_schema_version(adapter: SQLiteDevAdapter, bound_tenant) -> None:
    migrate(adapter)
    with adapter.with_connection() as conn:
        assert get_current_version(conn) >= 1


def test_migrate_001_creates_decisions_table(adapter: SQLiteDevAdapter, bound_tenant) -> None:
    migrate(adapter)
    with adapter.with_connection() as conn:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='decisions'",
        ).fetchone()
        assert row is not None


def test_migrate_001_creates_trust_snapshots_table(adapter: SQLiteDevAdapter, bound_tenant) -> None:
    migrate(adapter)
    with adapter.with_connection() as conn:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='trust_snapshots'",
        ).fetchone()
        assert row is not None


def test_migrate_001_creates_inherited_context_table(adapter: SQLiteDevAdapter, bound_tenant) -> None:
    migrate(adapter)
    with adapter.with_connection() as conn:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='inherited_context'",
        ).fetchone()
        assert row is not None


def test_migrate_001_inherited_context_has_hardened_column(adapter: SQLiteDevAdapter, bound_tenant) -> None:
    """v6 plan requires imports to land as hardened=0 shadow context.
    The column must exist + default to 0 so the split works from the
    first import."""
    migrate(adapter)
    with adapter.with_connection() as conn:
        cols = conn.execute("PRAGMA table_info(inherited_context)").fetchall()
        names = {row[1] for row in cols}
        assert "hardened" in names
        assert "hardened_at" in names


def test_migrate_rolls_back_on_syntax_error(adapter: SQLiteDevAdapter, bound_tenant, tmp_path: Path) -> None:
    broken = tmp_path / "broken_migrations"
    broken.mkdir()
    (broken / "001_initial.sql").write_text(
        "CREATE TABLE good (id INTEGER);\nNOT A VALID STATEMENT;",
        encoding="utf-8",
    )
    with pytest.raises(MigrationError):
        migrate(adapter, migrations_dir=broken)
    # The good table's CREATE must have rolled back with the bad statement.
    with adapter.with_connection() as conn:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='good'",
        ).fetchone()
        assert row is None


# ---------------------------------------------------------------------------
# Migration dataclass
# ---------------------------------------------------------------------------
def test_migration_sql_returns_file_content(tmp_path: Path) -> None:
    path = tmp_path / "001_hello.sql"
    path.write_text("SELECT 'hello';\n", encoding="utf-8")
    m = Migration(version=1, name="hello", path=path)
    assert m.sql() == "SELECT 'hello';\n"
