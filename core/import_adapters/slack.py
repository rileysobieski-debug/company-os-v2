"""Slack read-only import adapter.

Reads channel roster, user roster, and pinned messages in governance-
relevant channels from a tenant's Slack workspace. Does NOT ingest the
message stream: that would blow past Slack rate limits and pull
unstructured chatter into the Memory layer that the PVE cannot reason
about. Pinned messages are the signal that a channel's members have
agreed matters; that is what the chassis consumes.

Credentials format:

    {
      "bot_token": "xoxb-...",
      "workspace_id": "T...",
      "governance_channels": "C01ABC,C02DEF"  (comma-separated ids)
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


class SlackAdapter(ImportAdapter):
    name = "slack"

    REQUIRED_KEYS = ("bot_token", "workspace_id")

    def __init__(self, tenant_id: UUID, credentials: dict[str, str] | None = None) -> None:
        super().__init__(tenant_id, credentials)
        self._connected = False

    def connect(self) -> None:
        missing = [k for k in self.REQUIRED_KEYS if not self.credentials.get(k)]
        if missing:
            raise AdapterNotConfigured(
                f"SlackAdapter missing required credential keys: {missing}",
            )
        token = self.credentials.get("bot_token", "")
        if not token.startswith("xoxb-"):
            raise AdapterNotConfigured(
                "SlackAdapter bot_token should start with 'xoxb-' "
                "(bot user token, not user or app token)",
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
        token = self.credentials.get("bot_token", "")
        if not token.startswith("xoxb-"):
            return HealthResult(
                status=HealthStatus.NOT_CONFIGURED,
                detail="bot_token is not a valid bot user token (expected xoxb-...)",
                checked_at=now,
            )
        return HealthResult(
            status=HealthStatus.OK,
            detail=(
                "credential-shape valid; live auth.test probe lands in Weeks 6-7."
            ),
            checked_at=now,
        )

    def enumerate_entities(self) -> Iterable[EntityRef]:
        if not self._connected:
            raise AdapterNotConfigured(
                "SlackAdapter not connected; call connect() first",
            )
        # Real implementation yields: User, Channel, PinnedMessage.
        # Message stream is intentionally out of scope (rate limits +
        # noise).
        return iter(())

    def fetch_entity(self, entity_id: str) -> InheritedContext:
        if not self._connected:
            raise AdapterNotConfigured(
                "SlackAdapter not connected; call connect() first",
            )
        raise KeyError(
            f"SlackAdapter live fetch for {entity_id!r} lands in Weeks 6-7.",
        )
