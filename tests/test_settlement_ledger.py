"""
tests/test_settlement_ledger.py — Ticket 9 coverage
===================================================
Tests for `core.primitives.settlement_ledger.SettlementEventLedger` and
its integration with `MockSettlementAdapter`.

Covered:
- Round-trip: record N events, load_all() returns them in order with
  byte-identical fields.
- Markdown companion presence: for every JSONL line, a matching
  `<event_id>.md` exists.
- Atomicity: simulated crash (monkeypatched Path.replace raises) leaves
  events.jsonl byte-identical to its prior contents.
- Canonical determinism: semantically-equal events serialize to identical
  bytes regardless of dict-insertion order.
- Mock integration: lock + release produces two events in order with
  correct `kind` + linked `handle_id`.
- Lock + slash (both burn and beneficiary) emits lock then slash events.
- iter_events streams in disk order.
- Ledger=None default leaves behavior unchanged (no ledger file written).
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from core.primitives.asset import AssetRef
from core.primitives.money import Money
from core.primitives.settlement_adapters.mock_adapter import MockSettlementAdapter
from core.primitives.settlement_ledger import (
    LEDGER_FILENAME,
    SettlementEvent,
    SettlementEventLedger,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_event(
    *,
    kind: str = "lock",
    handle_id: str = "h0",
    asset_id: str = "mock-usd",
    amount: str = "1.000000",
    sla_id: str = "sla-x",
    outcome_receipt=None,
    metadata=None,
) -> SettlementEvent:
    return SettlementEvent(
        kind=kind,  # type: ignore[arg-type]
        handle_id=handle_id,
        asset_id=asset_id,
        amount_quantity_str=amount,
        sla_id=sla_id,
        principals={
            "requester_did": "",
            "provider_did": "",
            "counterparty_pubkey_hex": "",
        },
        outcome_receipt=outcome_receipt,
        metadata=metadata or {},
    )


# ---------------------------------------------------------------------------
# Ledger unit tests
# ---------------------------------------------------------------------------
def test_record_creates_dir_and_files(tmp_path: Path):
    ledger_dir = tmp_path / "ledger"
    ledger = SettlementEventLedger(ledger_dir)
    assert ledger_dir.exists()

    e = _make_event(handle_id="h1")
    ledger.record(e)

    assert ledger.jsonl_path.exists()
    assert ledger.md_path(e.event_id).exists()


def test_round_trip_three_events_preserves_order(tmp_path: Path):
    ledger = SettlementEventLedger(tmp_path)
    e1 = _make_event(kind="lock", handle_id="h1", amount="1.000000")
    e2 = _make_event(
        kind="release", handle_id="h1", amount="1.000000",
        outcome_receipt={"handle_id": "h1", "outcome": "released"},
    )
    e3 = _make_event(kind="lock", handle_id="h2", amount="2.500000")

    for ev in (e1, e2, e3):
        ledger.record(ev)

    loaded = ledger.load_all()
    assert [ev.event_id for ev in loaded] == [e1.event_id, e2.event_id, e3.event_id]
    assert [ev.kind for ev in loaded] == ["lock", "release", "lock"]
    assert [ev.handle_id for ev in loaded] == ["h1", "h1", "h2"]
    # Full-field equality via to_dict comparison
    assert [ev.to_dict() for ev in loaded] == [e1.to_dict(), e2.to_dict(), e3.to_dict()]


def test_markdown_companion_per_event(tmp_path: Path):
    ledger = SettlementEventLedger(tmp_path)
    events = [_make_event(handle_id=f"h{i}") for i in range(4)]
    for ev in events:
        ledger.record(ev)

    jsonl_lines = [
        json.loads(line)
        for line in ledger.jsonl_path.read_text("utf-8").splitlines()
        if line.strip()
    ]
    assert len(jsonl_lines) == len(events)
    for obj in jsonl_lines:
        md = ledger.md_path(obj["event_id"])
        assert md.exists(), f"missing markdown companion for {obj['event_id']}"
        text = md.read_text("utf-8")
        assert obj["event_id"] in text
        assert obj["kind"] in text


def test_atomicity_rename_failure_leaves_jsonl_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    ledger = SettlementEventLedger(tmp_path)
    # Seed with one successful event.
    first = _make_event(handle_id="good")
    ledger.record(first)
    before = ledger.jsonl_path.read_bytes()

    # Monkeypatch Path.replace to raise, simulating a crash mid-rename.
    original_replace = Path.replace

    def _boom(self, *args, **kwargs):
        if self.name.endswith(LEDGER_FILENAME + ".tmp"):
            raise OSError("simulated crash during rename")
        return original_replace(self, *args, **kwargs)

    monkeypatch.setattr(Path, "replace", _boom)

    bad = _make_event(handle_id="bad")
    with pytest.raises(OSError):
        ledger.record(bad)

    after = ledger.jsonl_path.read_bytes()
    assert after == before, "events.jsonl must be untouched when rename fails"
    # The bad event must not have been persisted.
    loaded = ledger.load_all()
    assert [ev.handle_id for ev in loaded] == ["good"]


def test_canonical_determinism_independent_of_dict_order():
    # Two semantically-equal events built with differently-ordered dicts.
    a = SettlementEvent(
        kind="release",
        handle_id="h",
        asset_id="mock-usd",
        amount_quantity_str="1.000000",
        sla_id="sla-1",
        principals={"provider_did": "p", "requester_did": "r", "counterparty_pubkey_hex": "c"},
        outcome_receipt={"outcome": "released", "handle_id": "h"},
        metadata={"z": 1, "a": 2},
        event_id="fixed-id",
        ts="2026-04-19T12:00:00Z",
    )
    b = SettlementEvent(
        kind="release",
        handle_id="h",
        asset_id="mock-usd",
        amount_quantity_str="1.000000",
        sla_id="sla-1",
        principals={"counterparty_pubkey_hex": "c", "requester_did": "r", "provider_did": "p"},
        outcome_receipt={"handle_id": "h", "outcome": "released"},
        metadata={"a": 2, "z": 1},
        event_id="fixed-id",
        ts="2026-04-19T12:00:00Z",
    )
    assert a.to_canonical_json() == b.to_canonical_json()


def test_iter_events_streams_in_disk_order(tmp_path: Path):
    ledger = SettlementEventLedger(tmp_path)
    ids: list[str] = []
    for i in range(5):
        ev = _make_event(handle_id=f"h{i}")
        ids.append(ev.event_id)
        ledger.record(ev)

    streamed_ids = [ev.event_id for ev in ledger.iter_events()]
    assert streamed_ids == ids


def test_iter_events_skips_malformed_lines(tmp_path: Path):
    ledger = SettlementEventLedger(tmp_path)
    ev = _make_event(handle_id="ok")
    ledger.record(ev)
    # Append a corrupt line directly — the ledger must not explode on read.
    with ledger.jsonl_path.open("a", encoding="utf-8") as f:
        f.write("{not valid json\n")
        f.write("\n")  # blank line
    loaded = ledger.load_all()
    assert [e.handle_id for e in loaded] == ["ok"]


# ---------------------------------------------------------------------------
# MockSettlementAdapter integration
# ---------------------------------------------------------------------------
def test_mock_lock_release_produces_two_linked_events(
    asset_registry, tmp_path: Path
):
    usd = asset_registry.get("mock-usd")
    ledger = SettlementEventLedger(tmp_path)
    adapter = MockSettlementAdapter((usd,), ledger=ledger)
    adapter.fund("alice", Money(Decimal("10"), usd))

    handle = adapter.lock(
        Money(Decimal("3"), usd),
        ref="sla-ab",
        nonce="n0",
        principal="alice",
    )
    adapter.release(handle, to="bob")

    events = ledger.load_all()
    assert [e.kind for e in events] == ["lock", "release"]
    assert all(e.handle_id == str(handle.handle_id) for e in events)
    assert events[0].outcome_receipt is None
    assert events[1].outcome_receipt is not None
    assert events[1].outcome_receipt["outcome"] == "released"
    assert events[0].sla_id == "sla-ab"
    assert events[1].sla_id == "sla-ab"


def test_mock_lock_slash_burn_produces_lock_then_slash(
    asset_registry, tmp_path: Path
):
    usd = asset_registry.get("mock-usd")
    ledger = SettlementEventLedger(tmp_path)
    adapter = MockSettlementAdapter((usd,), ledger=ledger)
    adapter.fund("alice", Money(Decimal("10"), usd))

    handle = adapter.lock(
        Money(Decimal("4"), usd),
        ref="sla-x",
        nonce="n1",
        principal="alice",
    )
    adapter.slash(handle, percent=50, beneficiary=None)

    events = ledger.load_all()
    assert [e.kind for e in events] == ["lock", "slash"]
    assert events[1].outcome_receipt is not None
    assert events[1].outcome_receipt["outcome"] == "slashed"
    assert events[1].metadata.get("beneficiary") == ""
    assert events[1].metadata.get("percent") == 50


def test_mock_lock_slash_beneficiary_writes_beneficiary_metadata(
    asset_registry, tmp_path: Path
):
    usd = asset_registry.get("mock-usd")
    ledger = SettlementEventLedger(tmp_path)
    adapter = MockSettlementAdapter((usd,), ledger=ledger)
    adapter.fund("alice", Money(Decimal("10"), usd))

    handle = adapter.lock(
        Money(Decimal("2"), usd),
        ref="sla-y",
        nonce="n2",
        principal="alice",
    )
    adapter.slash(handle, percent=25, beneficiary="carol")

    events = ledger.load_all()
    assert events[-1].kind == "slash"
    assert events[-1].metadata.get("beneficiary") == "carol"


def test_mock_no_ledger_default_behavior_unchanged(asset_registry, tmp_path: Path):
    """When the adapter has no ledger, it must not create any files and
    must behave identically to the pre-Ticket-9 implementation."""
    usd = asset_registry.get("mock-usd")
    adapter = MockSettlementAdapter((usd,))  # no ledger
    adapter.fund("alice", Money(Decimal("5"), usd))
    handle = adapter.lock(
        Money(Decimal("1"), usd),
        ref="ref",
        nonce="n-default",
        principal="alice",
    )
    receipt = adapter.release(handle, to="bob")

    assert receipt.outcome == "released"
    # No side-effect files anywhere in tmp_path
    assert list(tmp_path.iterdir()) == []


def test_new_event_kinds_roundtrip_through_jsonl(tmp_path: Path):
    """A5: all new EventKind values survive a JSONL write + reload cycle."""
    new_kinds = [
        "verdict_issued",
        "release_from_verdict",
        "slash_from_verdict",
        "refund_from_verdict",
        "founder_override",
    ]
    ledger = SettlementEventLedger(tmp_path)
    for kind in new_kinds:
        ev = _make_event(kind=kind, handle_id=f"h-{kind}")
        ledger.record(ev)

    loaded = ledger.load_all()
    assert len(loaded) == len(new_kinds)
    for original_kind, ev in zip(new_kinds, loaded):
        assert ev.kind == original_kind, (
            f"Expected kind {original_kind!r}, got {ev.kind!r}"
        )


def test_mock_principals_are_blank_by_default(asset_registry, tmp_path: Path):
    """Ticket 9 Option B: the mock adapter does not know DIDs, so it
    writes blanks. Ticket 6 will override via richer metadata."""
    usd = asset_registry.get("mock-usd")
    ledger = SettlementEventLedger(tmp_path)
    adapter = MockSettlementAdapter((usd,), ledger=ledger)
    adapter.fund("alice", Money(Decimal("1"), usd))
    adapter.lock(
        Money(Decimal("1"), usd),
        ref="r", nonce="n", principal="alice",
    )
    [event] = ledger.load_all()
    assert event.principals == {
        "requester_did": "",
        "provider_did": "",
        "counterparty_pubkey_hex": "",
    }
    # Locker name lands in metadata, not in principals.
    assert event.metadata.get("locker") == "alice"
