"""Import adapter tests.

Covers the three Week 2-3 read-only connectors (Notion, QuickBooks,
Slack) against their shared `ImportAdapter` contract. Live HTTP wiring
is deferred to Weeks 6-7; the contract + credential-shape handling +
write-blocking guards ship now.
"""
from __future__ import annotations

from uuid import uuid4

import pytest

from core.import_adapters import (
    AVAILABLE_ADAPTERS,
    AdapterNotConfigured,
    EntityRef,
    HealthStatus,
    ImportAdapter,
    InheritedContext,
    NotionAdapter,
    QuickBooksAdapter,
    ReadOnlyConnector,
    SlackAdapter,
)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
def test_available_adapters_covers_all_three() -> None:
    assert set(AVAILABLE_ADAPTERS.keys()) == {"notion", "quickbooks", "slack"}


def test_available_adapters_values_are_adapter_subclasses() -> None:
    for cls in AVAILABLE_ADAPTERS.values():
        assert issubclass(cls, ImportAdapter)


# ---------------------------------------------------------------------------
# Base contract: ImportAdapter is abstract
# ---------------------------------------------------------------------------
def test_import_adapter_is_abstract() -> None:
    with pytest.raises(TypeError):
        ImportAdapter(uuid4())  # type: ignore[abstract]


# ---------------------------------------------------------------------------
# Read-only guardrails (shared across all concrete adapters)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "adapter_cls,creds",
    [
        (NotionAdapter, {"integration_token": "secret_xyz", "workspace_id": "w1"}),
        (QuickBooksAdapter, {"access_token": "tok", "realm_id": "r1"}),
        (SlackAdapter, {"bot_token": "xoxb-abc", "workspace_id": "T1"}),
    ],
)
def test_write_raises_read_only_connector(adapter_cls, creds) -> None:
    adapter = adapter_cls(uuid4(), credentials=creds)
    with pytest.raises(ReadOnlyConnector):
        adapter.write("some", "payload")


@pytest.mark.parametrize(
    "adapter_cls,creds",
    [
        (NotionAdapter, {"integration_token": "secret_xyz", "workspace_id": "w1"}),
        (QuickBooksAdapter, {"access_token": "tok", "realm_id": "r1"}),
        (SlackAdapter, {"bot_token": "xoxb-abc", "workspace_id": "T1"}),
    ],
)
def test_delete_raises_read_only_connector(adapter_cls, creds) -> None:
    adapter = adapter_cls(uuid4(), credentials=creds)
    with pytest.raises(ReadOnlyConnector):
        adapter.delete("entity-id")


# ---------------------------------------------------------------------------
# Credentials handling
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "adapter_cls",
    [NotionAdapter, QuickBooksAdapter, SlackAdapter],
)
def test_credentials_are_defensively_copied(adapter_cls) -> None:
    creds = {"integration_token": "secret", "workspace_id": "w", "access_token": "t", "realm_id": "r", "bot_token": "xoxb-x"}
    adapter = adapter_cls(uuid4(), credentials=creds)
    # Mutating the original dict must not affect the adapter's view.
    creds["integration_token"] = "MUTATED"
    assert adapter.credentials.get("integration_token") != "MUTATED" or adapter.credentials.get("integration_token") == "secret"
    # And the property returns a copy too.
    view = adapter.credentials
    view["injected"] = "X"
    assert "injected" not in adapter.credentials


# ---------------------------------------------------------------------------
# NotionAdapter
# ---------------------------------------------------------------------------
def test_notion_connect_rejects_missing_token() -> None:
    adapter = NotionAdapter(uuid4(), credentials={"workspace_id": "w1"})
    with pytest.raises(AdapterNotConfigured):
        adapter.connect()


def test_notion_connect_rejects_missing_workspace() -> None:
    adapter = NotionAdapter(uuid4(), credentials={"integration_token": "s"})
    with pytest.raises(AdapterNotConfigured):
        adapter.connect()


def test_notion_connect_accepts_full_credentials() -> None:
    adapter = NotionAdapter(uuid4(), credentials={"integration_token": "s", "workspace_id": "w"})
    adapter.connect()


def test_notion_health_check_not_configured_when_missing() -> None:
    adapter = NotionAdapter(uuid4())
    result = adapter.health_check()
    assert result.status == HealthStatus.NOT_CONFIGURED


def test_notion_health_check_ok_when_configured() -> None:
    adapter = NotionAdapter(uuid4(), credentials={"integration_token": "s", "workspace_id": "w"})
    result = adapter.health_check()
    assert result.status == HealthStatus.OK


def test_notion_enumerate_without_connect_raises() -> None:
    adapter = NotionAdapter(uuid4(), credentials={"integration_token": "s", "workspace_id": "w"})
    with pytest.raises(AdapterNotConfigured):
        list(adapter.enumerate_entities())


def test_notion_fetch_unknown_id_raises_keyerror() -> None:
    adapter = NotionAdapter(uuid4(), credentials={"integration_token": "s", "workspace_id": "w"})
    adapter.connect()
    with pytest.raises(KeyError):
        adapter.fetch_entity("page-id-123")


# ---------------------------------------------------------------------------
# QuickBooksAdapter
# ---------------------------------------------------------------------------
def test_quickbooks_connect_rejects_missing_token() -> None:
    adapter = QuickBooksAdapter(uuid4(), credentials={"realm_id": "r1"})
    with pytest.raises(AdapterNotConfigured):
        adapter.connect()


def test_quickbooks_connect_accepts_full_credentials() -> None:
    adapter = QuickBooksAdapter(uuid4(), credentials={"access_token": "t", "realm_id": "r"})
    adapter.connect()


def test_quickbooks_health_check_includes_environment_label() -> None:
    adapter = QuickBooksAdapter(
        uuid4(),
        credentials={"access_token": "t", "realm_id": "r", "environment": "sandbox"},
    )
    result = adapter.health_check()
    assert result.status == HealthStatus.OK
    assert "sandbox" in result.detail


# ---------------------------------------------------------------------------
# SlackAdapter
# ---------------------------------------------------------------------------
def test_slack_connect_rejects_non_bot_token() -> None:
    # User tokens start with xoxp-, not xoxb-. The adapter must refuse.
    adapter = SlackAdapter(uuid4(), credentials={"bot_token": "xoxp-user", "workspace_id": "T1"})
    with pytest.raises(AdapterNotConfigured):
        adapter.connect()


def test_slack_connect_accepts_bot_token() -> None:
    adapter = SlackAdapter(uuid4(), credentials={"bot_token": "xoxb-valid", "workspace_id": "T1"})
    adapter.connect()


def test_slack_health_check_flags_wrong_token_type() -> None:
    adapter = SlackAdapter(uuid4(), credentials={"bot_token": "xoxp-user", "workspace_id": "T1"})
    result = adapter.health_check()
    assert result.status == HealthStatus.NOT_CONFIGURED


# ---------------------------------------------------------------------------
# Dataclass shape
# ---------------------------------------------------------------------------
def test_entity_ref_is_frozen() -> None:
    ref = EntityRef(adapter_name="notion", entity_id="p1", kind="page")
    with pytest.raises(Exception):
        ref.entity_id = "mutated"  # type: ignore[misc]


def test_inherited_context_is_frozen() -> None:
    ref = EntityRef(adapter_name="notion", entity_id="p1", kind="page")
    ctx = InheritedContext(ref=ref, payload={"k": "v"}, imported_at="2026-04-24T00:00:00+00:00")
    with pytest.raises(Exception):
        ctx.payload = {}  # type: ignore[misc]
