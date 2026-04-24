"""Versioned per-tenant schema migrations.

See `core.migrations.runner` for the application runtime. The `.sql`
files in this directory are the versioned migration source, numbered
in apply order. Each tenant's schema_meta table tracks the highest
applied version so `migrate(tenant_id)` is idempotent.
"""
