"""Read-only import adapters for transition-mode tenants.

Every adapter exposes the same contract (`core.import_adapters.base.ImportAdapter`)
so the Memory-layer ingestion code is provider-agnostic. Concrete
adapters shipped in v6 Week 2-3 sprint:

    - NotionAdapter: pages, databases, user roster.
    - QuickBooksAdapter: chart of accounts, vendors, invoices, P&L.
    - SlackAdapter: channel roster, user roster, pinned messages.

All three ship with the full public surface and credential shape so
downstream code can compile + test today. Live HTTP wiring is Weeks
6-7 scope per the v6 plan.

Usage:

    from core.import_adapters import NotionAdapter

    adapter = NotionAdapter(tenant_id, credentials={"integration_token": "...", "workspace_id": "..."})
    adapter.connect()
    for ref in adapter.enumerate_entities():
        ctx = adapter.fetch_entity(ref.entity_id)
        # hand ctx to Memory layer ingestion

Use `AVAILABLE_ADAPTERS` for dispatch-by-name when the caller has a
string source (e.g. from `TenantConfig.inherited_systems`).
"""
from __future__ import annotations

from core.import_adapters.base import (
    AdapterNotConfigured,
    EntityRef,
    HealthResult,
    HealthStatus,
    ImportAdapter,
    InheritedContext,
    ReadOnlyConnector,
)
from core.import_adapters.notion import NotionAdapter
from core.import_adapters.quickbooks import QuickBooksAdapter
from core.import_adapters.slack import SlackAdapter

AVAILABLE_ADAPTERS: dict[str, type[ImportAdapter]] = {
    "notion": NotionAdapter,
    "quickbooks": QuickBooksAdapter,
    "slack": SlackAdapter,
}

__all__ = [
    "AVAILABLE_ADAPTERS",
    "AdapterNotConfigured",
    "EntityRef",
    "HealthResult",
    "HealthStatus",
    "ImportAdapter",
    "InheritedContext",
    "NotionAdapter",
    "QuickBooksAdapter",
    "ReadOnlyConnector",
    "SlackAdapter",
]
