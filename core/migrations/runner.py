"""Migration runner for per-tenant schemas.

Loads `.sql` files from this directory in version order and applies
any unapplied migrations to the currently-bound tenant. `migrate()`
is idempotent: calling twice on an already-current tenant is a no-op.

Version tracking lives in the tenant's own `schema_meta` table under
the `schema_version` key (populated by `SQLiteDevAdapter.provision_tenant`
at version 0). Each migration ticks the value up by one and commits in
a single transaction with its DDL.

Migration filenames MUST match the pattern `NNN_description.sql`
where NNN is a zero-padded integer. Files that do not match are
ignored so auxiliary files (README, __init__.py, fixtures) can live
in the same directory without being interpreted as migrations.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from core.db_adapter import DatabaseAdapter, TenantNotBoundError

_MIGRATION_FILENAME = re.compile(r"^(\d{3})_([a-z0-9_]+)\.sql$")
_MIGRATIONS_DIR = Path(__file__).resolve().parent


class MigrationError(RuntimeError):
    """Raised when a migration cannot be applied."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    path: Path

    def sql(self) -> str:
        return self.path.read_text(encoding="utf-8")


def discover_migrations(migrations_dir: Path | None = None) -> list[Migration]:
    """Return every migration file in `migrations_dir`, sorted by
    version. Missing numbers in the sequence raise `MigrationError`
    so gaps cannot ship silently."""
    directory = migrations_dir or _MIGRATIONS_DIR
    out: list[Migration] = []
    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.suffix != ".sql":
            continue
        match = _MIGRATION_FILENAME.match(path.name)
        if not match:
            continue
        version = int(match.group(1))
        out.append(Migration(version=version, name=match.group(2), path=path))
    if not out:
        return out
    expected = 1
    for migration in out:
        if migration.version != expected:
            raise MigrationError(
                f"migration sequence gap: expected version {expected:03d}, "
                f"found {migration.version:03d} ({migration.name})",
            )
        expected += 1
    return out


def get_current_version(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT value FROM schema_meta WHERE key = 'schema_version'",
    ).fetchone()
    if row is None:
        raise MigrationError(
            "tenant DB is missing schema_meta.schema_version; "
            "was provision_tenant called?",
        )
    try:
        return int(row[0])
    except (TypeError, ValueError) as exc:
        raise MigrationError(
            f"schema_meta.schema_version is not an integer: {row[0]!r}",
        ) from exc


def _split_statements(script: str) -> list[str]:
    """Split a SQL script into individual complete statements without
    running them. Used instead of `sqlite3.Connection.executescript`
    because `executescript` issues an implicit COMMIT before running,
    which breaks the caller-owned BEGIN IMMEDIATE rollback path."""
    statements: list[str] = []
    buffer = ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            stmt = buffer.strip()
            if stmt:
                statements.append(stmt)
            buffer = ""
    leftover = buffer.strip()
    if leftover:
        statements.append(leftover)
    return statements


def apply_migration(conn: sqlite3.Connection, migration: Migration) -> None:
    """Apply a single migration and advance `schema_version`. Caller
    owns the connection's transaction boundary; this helper only
    executes the DDL + the version bump within that transaction."""
    for stmt in _split_statements(migration.sql()):
        conn.execute(stmt)
    conn.execute(
        "UPDATE schema_meta SET value = ? WHERE key = 'schema_version'",
        (str(migration.version),),
    )


def migrate(
    adapter: DatabaseAdapter,
    *,
    migrations_dir: Path | None = None,
) -> list[int]:
    """Apply any unapplied migrations to the currently-bound tenant.
    Returns the list of version numbers actually applied (empty if the
    tenant was already current). Raises `TenantNotBoundError` if no
    tenant is bound on the current thread."""
    if adapter.current_tenant() is None:
        raise TenantNotBoundError(
            "migrate() requires a bound tenant; "
            "wrap the call in `with_tenant_schema(tenant_id):`",
        )
    migrations = discover_migrations(migrations_dir)
    applied: list[int] = []
    with adapter.with_connection() as conn:
        current = get_current_version(conn)
        for migration in migrations:
            if migration.version <= current:
                continue
            try:
                conn.execute("BEGIN IMMEDIATE")
                apply_migration(conn, migration)
                conn.commit()
            except sqlite3.Error as exc:
                conn.rollback()
                raise MigrationError(
                    f"migration {migration.version:03d} "
                    f"({migration.name}) failed: {exc}",
                ) from exc
            applied.append(migration.version)
            current = migration.version
    return applied
