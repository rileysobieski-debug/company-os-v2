"""Base contract for read-only import adapters.

Transition-mode tenants bring existing state from legacy SaaS into the
chassis via `ImportAdapter` subclasses. Every adapter is read-only at
the connector surface: any attempt to call a write-shaped method
raises `ReadOnlyConnector`. Write paths always go through the Memory
layer so the provenance chain is intact.

Adapter data lands in the tenant's `inherited_context` table (see
migration 001) with `hardened=0`. The 14-day decay worker or explicit
founder hardening flips `hardened=1` at which point the row starts
feeding the Brain's hard-constraint gate. This split is v6 Gemini-round
resolution for the "non-deterministic fuel source" problem.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable
from uuid import UUID


class AdapterNotConfigured(RuntimeError):
    """Raised when an import adapter is invoked without valid
    credentials or connection parameters. Callers catch and surface
    to the onboarding UI as a missing-integration Tension event."""


class ReadOnlyConnector(RuntimeError):
    """Raised when a caller attempts a write through a read-only
    adapter. Every ImportAdapter subclass MUST be read-only at the
    connector surface."""


class HealthStatus(Enum):
    OK = "ok"
    DEGRADED = "degraded"
    UNREACHABLE = "unreachable"
    NOT_CONFIGURED = "not_configured"


@dataclass(frozen=True)
class HealthResult:
    status: HealthStatus
    detail: str
    checked_at: str  # ISO-8601 UTC


@dataclass(frozen=True)
class EntityRef:
    """Stable pointer to an entity inside a source system. The adapter
    decides the id scheme (Notion page id, QuickBooks account number,
    Slack channel id, etc.) but it must be stable: two enumerations of
    the same source system must return the same id for the same entity
    so the tenant's inherited_context table can UPSERT cleanly."""
    adapter_name: str
    entity_id: str
    kind: str
    label: str = ""


@dataclass(frozen=True)
class InheritedContext:
    """One row's worth of imported content. Payload is opaque JSON; the
    Memory-layer ingestion step is what normalizes it into Claims."""
    ref: EntityRef
    payload: dict[str, Any]
    imported_at: str
    source_asof: str | None = None  # when the source system last modified
    tags: tuple[str, ...] = field(default_factory=tuple)


class ImportAdapter(ABC):
    """Read-only connector contract. Concrete adapters live in sibling
    modules (notion.py, quickbooks.py, slack.py). Every adapter declares
    a `name` class attribute so logs and inherited_context rows can
    attribute content back to the source without a mapping table."""

    name: str = "base"

    def __init__(self, tenant_id: UUID, credentials: dict[str, str] | None = None) -> None:
        self._tenant_id = tenant_id
        self._credentials = dict(credentials or {})

    # ---- required contract ----------------------------------------------
    @abstractmethod
    def connect(self) -> None:
        """Establish the connection. Raises `AdapterNotConfigured` when
        credentials are missing or invalid."""

    @abstractmethod
    def health_check(self) -> HealthResult:
        """Fast probe against the source system. Must not raise; wrap
        transient errors into `HealthStatus.DEGRADED` or `UNREACHABLE`."""

    @abstractmethod
    def enumerate_entities(self) -> Iterable[EntityRef]:
        """Yield every governance-relevant entity in the source system.
        Implementations MUST respect source-system rate limits and stop
        short of pulling entire message histories; the goal is a useful
        index, not an exhaustive mirror."""

    @abstractmethod
    def fetch_entity(self, entity_id: str) -> InheritedContext:
        """Resolve a single entity by id. Raises `KeyError` when the
        entity has been deleted upstream since last enumerate."""

    # ---- write-blocking guard rails ------------------------------------
    def write(self, *args: object, **kwargs: object) -> None:
        raise ReadOnlyConnector(
            f"{type(self).__name__} is a read-only import adapter; "
            "writes always go through the Memory layer, never through "
            "the source system.",
        )

    def delete(self, *args: object, **kwargs: object) -> None:
        raise ReadOnlyConnector(
            f"{type(self).__name__} does not support delete; "
            "the source system is authoritative for deletions.",
        )

    # ---- accessors ------------------------------------------------------
    @property
    def tenant_id(self) -> UUID:
        return self._tenant_id

    @property
    def credentials(self) -> dict[str, str]:
        """Return a defensive copy so callers cannot mutate internal state."""
        return dict(self._credentials)
