"""QuickBooks read-only import adapter.

Reads chart of accounts, vendor list, open contracts, and historical
P&L from a tenant's QuickBooks Online company file. Real OAuth + HTTP
wiring is Weeks 6-7 scope; this module ships the shape so the Memory
layer can compile against it today.

QuickBooks is the canonical transition-mode integration. Finance-heavy
tenants are the most likely to run QuickBooks; the v6 LegacyInvoiceHold
primitive (governance hold on a QuickBooks invoice without mutating
the source) depends on this adapter for read-side visibility.

Credentials format:

    {
      "access_token": "...",
      "realm_id": "...",
      "refresh_token": "...",
      "environment": "sandbox" | "production",
    }
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable
from uuid import UUID

from core.import_adapters.base import (
    AdapterNotConfigured,
    EntityRef,
    HealthResult,
    HealthStatus,
    ImportAdapter,
    InheritedContext,
)


class QuickBooksAdapter(ImportAdapter):
    name = "quickbooks"

    REQUIRED_KEYS = ("access_token", "realm_id")

    def __init__(self, tenant_id: UUID, credentials: dict[str, str] | None = None) -> None:
        super().__init__(tenant_id, credentials)
        self._connected = False

    def connect(self) -> None:
        missing = [k for k in self.REQUIRED_KEYS if not self.credentials.get(k)]
        if missing:
            raise AdapterNotConfigured(
                f"QuickBooksAdapter missing required credential keys: {missing}",
            )
        self._connected = True

    def health_check(self) -> HealthResult:
        now = datetime.now(timezone.utc).isoformat()
        missing = [k for k in self.REQUIRED_KEYS if not self.credentials.get(k)]
        if missing:
            return HealthResult(
                status=HealthStatus.NOT_CONFIGURED,
                detail=f"missing credential keys: {missing}",
                checked_at=now,
            )
        env = self.credentials.get("environment", "sandbox")
        return HealthResult(
            status=HealthStatus.OK,
            detail=(
                f"credential-shape valid for {env} environment; "
                "live OAuth refresh + Company Info probe lands in Weeks 6-7."
            ),
            checked_at=now,
        )

    def enumerate_entities(self) -> Iterable[EntityRef]:
        if not self._connected:
            raise AdapterNotConfigured(
                "QuickBooksAdapter not connected; call connect() first",
            )
        # Real implementation yields: Account (chart of accounts rows),
        # Vendor, Customer, Invoice (for LegacyInvoiceHold), and the
        # Profit & Loss report as a single synthetic entity.
        return iter(())

    def fetch_entity(self, entity_id: str) -> InheritedContext:
        if not self._connected:
            raise AdapterNotConfigured(
                "QuickBooksAdapter not connected; call connect() first",
            )
        raise KeyError(
            f"QuickBooksAdapter live fetch for {entity_id!r} lands in "
            "Weeks 6-7; stub refuses synthetic payloads.",
        )
