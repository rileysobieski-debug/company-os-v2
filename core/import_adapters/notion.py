"""Notion read-only import adapter.

Pulls pages, databases, and user roster from a tenant's Notion
workspace. Real HTTP wiring is Weeks 6-7 scope; this module ships the
full public surface and credential handling so the downstream Memory-
layer code can compile and test against a realistic shape today.

Credentials format (set via onboarding):

    {
      "integration_token": "secret_...",
      "workspace_id": "...",
    }

The integration token is stored in KMS per the v6 plan (Gemini-round
OAuth-token-storage finding); this adapter receives it via the
`credentials` dict only at call time, never persisted locally.
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


class NotionAdapter(ImportAdapter):
    name = "notion"

    REQUIRED_KEYS = ("integration_token", "workspace_id")

    def __init__(self, tenant_id: UUID, credentials: dict[str, str] | None = None) -> None:
        super().__init__(tenant_id, credentials)
        self._connected = False

    def connect(self) -> None:
        missing = [k for k in self.REQUIRED_KEYS if not self.credentials.get(k)]
        if missing:
            raise AdapterNotConfigured(
                f"NotionAdapter missing required credential keys: {missing}",
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
        return HealthResult(
            status=HealthStatus.OK,
            detail=(
                "credential-shape valid; live HTTP health probe ships in "
                "Weeks 6-7 along with the real Notion SDK wiring."
            ),
            checked_at=now,
        )

    def enumerate_entities(self) -> Iterable[EntityRef]:
        if not self._connected:
            raise AdapterNotConfigured(
                "NotionAdapter not connected; call connect() first",
            )
        # Real implementation (Weeks 6-7) iterates the Notion search API,
        # paginates, and surfaces `page`, `database`, `user` entities.
        # Until then this yields nothing so the Memory layer can compile
        # and exercise the downstream path with an empty import batch.
        return iter(())

    def fetch_entity(self, entity_id: str) -> InheritedContext:
        if not self._connected:
            raise AdapterNotConfigured(
                "NotionAdapter not connected; call connect() first",
            )
        raise KeyError(
            f"NotionAdapter live fetch for {entity_id!r} is a Weeks 6-7 "
            "deliverable; no stub payload is shipped to avoid Memory "
            "layer ingesting synthetic provenance.",
        )
