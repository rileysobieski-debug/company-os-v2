"""
tests/test_mock_adapter.py -- Ticket 3 + A5 coverage
====================================================
Tests for `core.primitives.settlement_adapters.mock_adapter.MockSettlementAdapter`.

Covered (original Ticket 3):
- supports() advertises capability correctly
- lock freezes balance; release transfers, emits valid SettlementReceipt
- get_status transitions locked -> released / slashed
- slash with burn (beneficiary=None): burns slash_amount, remainder to locker
- slash with beneficiary: transferred to beneficiary, remainder to locker
- double-release raises EscrowStateError
- get_status on unknown handle raises EscrowStateError
- unsupported asset raises UnsupportedAssetError in lock / fund
- balance() on unseen principal returns zero-Money
- nonce replay rejected across re-used nonces
- distinct nonces succeed independently
- multi-asset support: one adapter handles USD + EUR with separate balances
- SettlementReceipt.ts matches canonical UTC-Z form

Covered (Ticket A5 -- release_pending_verdict):
- accepted path: escrow released to provider, correct ledger sequence
- rejected path: 100% slash to requester, correct ledger sequence
- refunded path: escrow returned to locker, correct ledger sequence
- double machine verdict raises VerdictError
- founder override path: rejected Tier 0 then accepted Tier 3 releases correctly,
  ledger sequence: lock -> verdict_issued(tier0) -> slash_from_verdict ->
  verdict_issued(tier3) -> founder_override -> release_from_verdict
- tampered verdict raises SignatureError
- mismatched sla_id raises VerdictError
- mismatched artifact_hash raises VerdictError
- StablecoinStubAdapter.release_pending_verdict raises NotImplementedError
"""
from __future__ import annotations

import dataclasses
import hashlib
import re
from decimal import Decimal
from pathlib import Path

import pytest

from core.primitives.asset import AssetRef
from core.primitives.exceptions import (
    EscrowStateError,
    SignatureError,
    UnsupportedAssetError,
    VerdictError,
)
from core.primitives.identity import Ed25519Keypair
from core.primitives.money import Money
from core.primitives.oracle import Oracle, OracleVerdict
from core.primitives.schema_verifier import SchemaVerifier
from core.primitives.settlement_adapters.mock_adapter import MockSettlementAdapter
from core.primitives.settlement_adapters.stablecoin_stub import StablecoinStubAdapter
from core.primitives.settlement_ledger import SettlementEventLedger
from core.primitives.signer import LocalKeypairSigner
from core.primitives.sla import InterOrgSLA


_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


# ---------------------------------------------------------------------------
# Capability + unsupported assets
# ---------------------------------------------------------------------------
def test_supports_reports_configured_assets(asset_registry):
    usd = asset_registry.get("mock-usd")
    eur = asset_registry.get("mock-eur")
    adapter = MockSettlementAdapter((usd,))
    assert adapter.supports(usd) is True
    assert adapter.supports(eur) is False


def test_lock_unsupported_asset_raises(asset_registry):
    usd = asset_registry.get("mock-usd")
    eur = asset_registry.get("mock-eur")
    adapter = MockSettlementAdapter((usd,))
    with pytest.raises(UnsupportedAssetError):
        adapter.lock(
            Money(Decimal("1"), eur),
            ref="x",
            nonce="n0",
            principal="alice",
        )


def test_fund_unsupported_asset_raises(asset_registry):
    usd = asset_registry.get("mock-usd")
    eur = asset_registry.get("mock-eur")
    adapter = MockSettlementAdapter((usd,))
    with pytest.raises(UnsupportedAssetError):
        adapter.fund("alice", Money(Decimal("1"), eur))


def test_constructor_requires_at_least_one_asset():
    with pytest.raises(ValueError):
        MockSettlementAdapter(())


# ---------------------------------------------------------------------------
# Balance
# ---------------------------------------------------------------------------
def test_balance_unseen_principal_returns_zero(asset_registry):
    usd = asset_registry.get("mock-usd")
    adapter = MockSettlementAdapter((usd,))
    bal = adapter.balance("nobody", usd)
    assert bal == Money.zero(usd)


def test_fund_then_balance(asset_registry):
    usd = asset_registry.get("mock-usd")
    adapter = MockSettlementAdapter((usd,))
    adapter.fund("alice", Money(Decimal("100"), usd))
    assert adapter.balance("alice", usd) == Money(Decimal("100"), usd)


# ---------------------------------------------------------------------------
# Lock / release happy path
# ---------------------------------------------------------------------------
def test_lock_freezes_balance(asset_registry):
    usd = asset_registry.get("mock-usd")
    adapter = MockSettlementAdapter((usd,))
    adapter.fund("alice", Money(Decimal("100"), usd))

    handle = adapter.lock(
        Money(Decimal("30"), usd),
        ref="sla-1",
        nonce="n1",
        principal="alice",
    )

    # Balance debited; escrow status is "locked".
    assert adapter.balance("alice", usd) == Money(Decimal("70"), usd)
    assert adapter.get_status(handle) == "locked"
    assert handle.locked_amount == Money(Decimal("30"), usd)
    assert handle.ref == "sla-1"


def test_release_transfers_to_destination(asset_registry):
    usd = asset_registry.get("mock-usd")
    adapter = MockSettlementAdapter((usd,))
    adapter.fund("alice", Money(Decimal("100"), usd))
    handle = adapter.lock(
        Money(Decimal("30"), usd),
        ref="sla-1",
        nonce="n1",
        principal="alice",
    )
    receipt = adapter.release(handle, to="bob")

    assert receipt.outcome == "released"
    assert receipt.to == "bob"
    assert receipt.transferred == Money(Decimal("30"), usd)
    assert receipt.burned == Money.zero(usd)
    assert _TS_RE.match(receipt.ts), f"ts not canonical UTC-Z: {receipt.ts!r}"
    assert receipt.handle_id == handle.handle_id

    assert adapter.balance("bob", usd) == Money(Decimal("30"), usd)
    assert adapter.get_status(handle) == "released"


def test_lock_insufficient_balance_raises(asset_registry):
    usd = asset_registry.get("mock-usd")
    adapter = MockSettlementAdapter((usd,))
    adapter.fund("alice", Money(Decimal("10"), usd))
    with pytest.raises(ValueError):
        adapter.lock(
            Money(Decimal("50"), usd),
            ref="sla-1",
            nonce="n1",
            principal="alice",
        )


# ---------------------------------------------------------------------------
# Slash — burn and beneficiary
# ---------------------------------------------------------------------------
def test_slash_with_burn_beneficiary_none(asset_registry):
    usd = asset_registry.get("mock-usd")
    adapter = MockSettlementAdapter((usd,))
    adapter.fund("alice", Money(Decimal("100"), usd))
    handle = adapter.lock(
        Money(Decimal("40"), usd),
        ref="sla-1",
        nonce="n1",
        principal="alice",
    )

    receipt = adapter.slash(handle, percent=25, beneficiary=None)

    # 25% of 40 = 10 burned; remainder 30 returns to alice.
    assert receipt.outcome == "slashed"
    assert receipt.to == ""
    assert receipt.transferred == Money.zero(usd)
    assert receipt.burned == Money(Decimal("10"), usd)
    assert _TS_RE.match(receipt.ts)

    # Alice started with 100, locked 40 (balance 60), got 30 back.
    assert adapter.balance("alice", usd) == Money(Decimal("90"), usd)
    assert adapter.get_status(handle) == "slashed"


def test_slash_with_beneficiary(asset_registry):
    usd = asset_registry.get("mock-usd")
    adapter = MockSettlementAdapter((usd,))
    adapter.fund("alice", Money(Decimal("100"), usd))
    handle = adapter.lock(
        Money(Decimal("40"), usd),
        ref="sla-1",
        nonce="n1",
        principal="alice",
    )

    receipt = adapter.slash(handle, percent=25, beneficiary="carol")

    assert receipt.outcome == "slashed"
    assert receipt.to == "carol"
    assert receipt.transferred == Money(Decimal("10"), usd)
    assert receipt.burned == Money.zero(usd)

    assert adapter.balance("carol", usd) == Money(Decimal("10"), usd)
    # Alice: 100 - 40 locked + 30 returned = 90
    assert adapter.balance("alice", usd) == Money(Decimal("90"), usd)
    assert adapter.get_status(handle) == "slashed"


def test_slash_percent_out_of_range_raises(asset_registry):
    usd = asset_registry.get("mock-usd")
    adapter = MockSettlementAdapter((usd,))
    adapter.fund("alice", Money(Decimal("100"), usd))
    handle = adapter.lock(
        Money(Decimal("40"), usd),
        ref="sla-1",
        nonce="n1",
        principal="alice",
    )
    with pytest.raises(ValueError):
        adapter.slash(handle, percent=150, beneficiary=None)
    with pytest.raises(ValueError):
        adapter.slash(handle, percent=-1, beneficiary=None)


# ---------------------------------------------------------------------------
# Error transitions
# ---------------------------------------------------------------------------
def test_double_release_raises(asset_registry):
    usd = asset_registry.get("mock-usd")
    adapter = MockSettlementAdapter((usd,))
    adapter.fund("alice", Money(Decimal("100"), usd))
    handle = adapter.lock(
        Money(Decimal("30"), usd),
        ref="sla-1",
        nonce="n1",
        principal="alice",
    )
    adapter.release(handle, to="bob")
    with pytest.raises(EscrowStateError):
        adapter.release(handle, to="bob")


def test_slash_after_release_raises(asset_registry):
    usd = asset_registry.get("mock-usd")
    adapter = MockSettlementAdapter((usd,))
    adapter.fund("alice", Money(Decimal("100"), usd))
    handle = adapter.lock(
        Money(Decimal("30"), usd),
        ref="sla-1",
        nonce="n1",
        principal="alice",
    )
    adapter.release(handle, to="bob")
    with pytest.raises(EscrowStateError):
        adapter.slash(handle, percent=50, beneficiary=None)


def test_get_status_unknown_handle_raises(asset_registry):
    from core.primitives.settlement_adapters import EscrowHandle, EscrowHandleId
    usd = asset_registry.get("mock-usd")
    adapter = MockSettlementAdapter((usd,))
    bogus = EscrowHandle(
        handle_id=EscrowHandleId("deadbeef"),
        asset=usd,
        locked_amount=Money(Decimal("1"), usd),
        ref="x",
    )
    with pytest.raises(EscrowStateError):
        adapter.get_status(bogus)


def test_release_unknown_handle_raises(asset_registry):
    from core.primitives.settlement_adapters import EscrowHandle, EscrowHandleId
    usd = asset_registry.get("mock-usd")
    adapter = MockSettlementAdapter((usd,))
    bogus = EscrowHandle(
        handle_id=EscrowHandleId("deadbeef"),
        asset=usd,
        locked_amount=Money(Decimal("1"), usd),
        ref="x",
    )
    with pytest.raises(EscrowStateError):
        adapter.release(bogus, to="bob")


# ---------------------------------------------------------------------------
# Replay resistance
# ---------------------------------------------------------------------------
def test_nonce_replay_rejected(asset_registry):
    """Second lock with the same nonce must raise, even with other fields different."""
    usd = asset_registry.get("mock-usd")
    adapter = MockSettlementAdapter((usd,))
    adapter.fund("alice", Money(Decimal("100"), usd))

    adapter.lock(
        Money(Decimal("10"), usd),
        ref="sla-1",
        nonce="same-nonce",
        principal="alice",
    )
    with pytest.raises(EscrowStateError) as excinfo:
        adapter.lock(
            Money(Decimal("20"), usd),  # different amount
            ref="sla-2",                  # different ref
            nonce="same-nonce",          # reused nonce
            principal="alice",
        )
    assert "nonce replay" in str(excinfo.value)


def test_distinct_nonces_succeed(asset_registry):
    usd = asset_registry.get("mock-usd")
    adapter = MockSettlementAdapter((usd,))
    adapter.fund("alice", Money(Decimal("100"), usd))

    h1 = adapter.lock(
        Money(Decimal("10"), usd),
        ref="sla-1",
        nonce="nonce-a",
        principal="alice",
    )
    h2 = adapter.lock(
        Money(Decimal("15"), usd),
        ref="sla-2",
        nonce="nonce-b",
        principal="alice",
    )
    assert h1.handle_id != h2.handle_id
    assert adapter.get_status(h1) == "locked"
    assert adapter.get_status(h2) == "locked"


def test_nonce_consumed_even_when_lock_other_checks_would_fail(asset_registry):
    """Design choice: replay check fires FIRST. This prevents attackers
    from probing whether a nonce was used by looking at error ordering."""
    usd = asset_registry.get("mock-usd")
    adapter = MockSettlementAdapter((usd,))
    adapter.fund("alice", Money(Decimal("100"), usd))
    adapter.lock(
        Money(Decimal("10"), usd),
        ref="sla-1",
        nonce="nonce-x",
        principal="alice",
    )
    # Reuse nonce AND unsupported asset. Must raise replay, not unsupported.
    eur = AssetRef(asset_id="mock-eur", contract="EUR", decimals=2)
    with pytest.raises(EscrowStateError) as excinfo:
        adapter.lock(
            Money(Decimal("1"), eur),
            ref="sla-2",
            nonce="nonce-x",
            principal="alice",
        )
    assert "nonce replay" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Multi-asset support
# ---------------------------------------------------------------------------
def test_multi_asset_adapter_keeps_balances_separate(asset_registry):
    usd = asset_registry.get("mock-usd")
    eur = asset_registry.get("mock-eur")
    adapter = MockSettlementAdapter((usd, eur))

    adapter.fund("alice", Money(Decimal("100"), usd))
    adapter.fund("alice", Money(Decimal("50"), eur))

    h_usd = adapter.lock(
        Money(Decimal("30"), usd),
        ref="sla-usd",
        nonce="n-usd",
        principal="alice",
    )
    h_eur = adapter.lock(
        Money(Decimal("20"), eur),
        ref="sla-eur",
        nonce="n-eur",
        principal="alice",
    )

    assert adapter.balance("alice", usd) == Money(Decimal("70"), usd)
    assert adapter.balance("alice", eur) == Money(Decimal("30"), eur)

    rcpt_usd = adapter.release(h_usd, to="bob")
    rcpt_eur = adapter.release(h_eur, to="bob")

    assert rcpt_usd.transferred == Money(Decimal("30"), usd)
    assert rcpt_eur.transferred == Money(Decimal("20"), eur)
    assert adapter.balance("bob", usd) == Money(Decimal("30"), usd)
    assert adapter.balance("bob", eur) == Money(Decimal("20"), eur)


# ---------------------------------------------------------------------------
# Forward-compat kwargs
# ---------------------------------------------------------------------------
def test_ledger_kwarg_default_none_no_writes(asset_registry):
    """Ticket 9: default `ledger=None` means no event emission. The
    adapter behaves identically to the pre-Ticket-9 code path."""
    usd = asset_registry.get("mock-usd")
    adapter = MockSettlementAdapter((usd,))
    assert adapter._ledger is None
    adapter.fund("alice", Money(Decimal("10"), usd))
    handle = adapter.lock(
        Money(Decimal("5"), usd),
        ref="sla-1",
        nonce="n1",
        principal="alice",
    )
    # Release and slash paths also work without a ledger.
    adapter.release(handle, to="bob")


# ---------------------------------------------------------------------------
# A5 helpers
# ---------------------------------------------------------------------------
def _valid_schema_envelope() -> dict:
    return {
        "kind": "json_schema",
        "spec_version": "2020-12",
        "schema": {
            "type": "object",
            "properties": {"summary": {"type": "string"}},
            "required": ["summary"],
        },
    }


def _make_sla_with_hash(artifact_bytes: bytes, *, usd: AssetRef) -> InterOrgSLA:
    """Build a minimal SLA with delivery hash populated."""
    payment = Money(Decimal("100.000000"), usd)
    sla = InterOrgSLA.create(
        sla_id="test-sla-a5-001",
        requester_node_did="did:test:requester",
        provider_node_did="did:test:provider",
        task_scope="A5 adapter integration test",
        deliverable_schema=_valid_schema_envelope(),
        accuracy_requirement=0.9,
        latency_ms=60_000,
        payment=payment,
        penalty_stake=payment,
        nonce=InterOrgSLA.new_nonce(),
        issued_at="2026-04-21T00:00:00Z",
        expires_at="2026-04-28T00:00:00Z",
    )
    artifact_hash = hashlib.sha256(artifact_bytes).hexdigest()
    return sla.with_delivery_hash(artifact_hash)


def _adapter_with_locked_escrow(
    usd: AssetRef,
    ledger: SettlementEventLedger,
    sla: InterOrgSLA,
    *,
    nonce: str = "nonce-v1",
) -> tuple[MockSettlementAdapter, object]:
    """Fund requester and lock escrow against sla_id. Returns (adapter, handle)."""
    adapter = MockSettlementAdapter((usd,), ledger=ledger)
    adapter.fund("did:test:requester", Money(Decimal("100.000000"), usd))
    handle = adapter.lock(
        Money(Decimal("100.000000"), usd),
        ref=sla.sla_id,
        nonce=nonce,
        principal="did:test:requester",
    )
    return adapter, handle


# ---------------------------------------------------------------------------
# A5: release_pending_verdict -- three result paths
# ---------------------------------------------------------------------------
def test_release_pending_verdict_accepted(asset_registry, tmp_path: Path):
    """accepted: escrow released to provider; ledger sequence lock -> verdict_issued -> release_from_verdict."""
    usd = asset_registry.get("mock-usd")
    ledger = SettlementEventLedger(tmp_path)

    node_kp = Ed25519Keypair.generate()
    # Artifact satisfies schema (has "summary" field) -> verdict result = accepted.
    artifact_bytes = b'{"summary": "all good"}'
    sla = _make_sla_with_hash(artifact_bytes, usd=usd)

    oracle = Oracle(
        node_did="did:test:oracle",
        node_keypair=node_kp,
        schema_verifier=SchemaVerifier(),
    )
    verdict = oracle.evaluate_tier0(sla, artifact_bytes)
    assert verdict.result == "accepted"

    adapter, handle = _adapter_with_locked_escrow(usd, ledger, sla)

    receipt = adapter.release_pending_verdict(
        handle,
        verdict,
        expected_artifact_hash=sla.artifact_hash_at_delivery,
        requester_did="did:test:requester",
        provider_did="did:test:provider",
    )

    assert receipt.outcome == "released"
    assert receipt.to == "did:test:provider"
    assert receipt.transferred == Money(Decimal("100.000000"), usd)

    # Provider credited, requester unchanged (had 100 - 100 locked = 0).
    assert adapter.balance("did:test:provider", usd) == Money(Decimal("100.000000"), usd)
    assert adapter.get_status(handle) == "released"

    events = ledger.load_all()
    kinds = [e.kind for e in events]
    assert kinds == ["lock", "verdict_issued", "release_from_verdict"]
    assert events[1].metadata["result"] == "accepted"
    assert events[1].metadata["verdict_hash"] == verdict.verdict_hash


def test_release_pending_verdict_rejected(asset_registry, tmp_path: Path):
    """rejected: 100% slash to requester; ledger sequence lock -> verdict_issued -> slash_from_verdict."""
    usd = asset_registry.get("mock-usd")
    ledger = SettlementEventLedger(tmp_path)

    node_kp = Ed25519Keypair.generate()
    # Artifact missing required "summary" field -> rejected verdict from schema verifier.
    artifact_bytes = b'{"missing_summary": true}'
    sla = _make_sla_with_hash(artifact_bytes, usd=usd)
    artifact_hash = hashlib.sha256(artifact_bytes).hexdigest()

    verdict = OracleVerdict.create(
        sla_id=sla.sla_id,
        artifact_hash=artifact_hash,
        tier=0,
        result="rejected",
        evaluator_did="did:test:oracle-node",
        evidence={"kind": "schema_fail", "error": "failed validation"},
        issued_at="2026-04-21T00:00:00Z",
        signer=LocalKeypairSigner(node_kp),
    )

    adapter, handle = _adapter_with_locked_escrow(usd, ledger, sla)
    receipt = adapter.release_pending_verdict(
        handle,
        verdict,
        expected_artifact_hash=sla.artifact_hash_at_delivery,
        requester_did="did:test:requester",
        provider_did="did:test:provider",
    )

    assert receipt.outcome == "slashed"
    assert receipt.to == "did:test:requester"
    assert receipt.transferred == Money(Decimal("100.000000"), usd)

    # Requester gets back the slashed amount (100% slash to requester = transfer back).
    assert adapter.balance("did:test:requester", usd) == Money(Decimal("100.000000"), usd)
    assert adapter.balance("did:test:provider", usd) == Money.zero(usd)
    assert adapter.get_status(handle) == "slashed"

    events = ledger.load_all()
    kinds = [e.kind for e in events]
    assert kinds == ["lock", "verdict_issued", "slash_from_verdict"]
    assert events[1].metadata["result"] == "rejected"


def test_release_pending_verdict_refunded(asset_registry, tmp_path: Path):
    """refunded: escrow returned to locker, no slash; ledger sequence lock -> verdict_issued -> refund_from_verdict."""
    usd = asset_registry.get("mock-usd")
    ledger = SettlementEventLedger(tmp_path)

    node_kp = Ed25519Keypair.generate()
    artifact_bytes = b'{"parse_error": true}'
    sla = _make_sla_with_hash(artifact_bytes, usd=usd)
    artifact_hash = hashlib.sha256(artifact_bytes).hexdigest()

    verdict = OracleVerdict.create(
        sla_id=sla.sla_id,
        artifact_hash=artifact_hash,
        tier=0,
        result="refunded",
        evaluator_did="did:test:oracle-node",
        evidence={"kind": "artifact_parse_error", "error": "could not decode"},
        issued_at="2026-04-21T00:00:00Z",
        signer=LocalKeypairSigner(node_kp),
    )

    adapter, handle = _adapter_with_locked_escrow(usd, ledger, sla)
    receipt = adapter.release_pending_verdict(
        handle,
        verdict,
        expected_artifact_hash=sla.artifact_hash_at_delivery,
        requester_did="did:test:requester",
        provider_did="did:test:provider",
    )

    assert receipt.outcome == "released"
    assert receipt.to == "did:test:requester"
    assert receipt.transferred == Money(Decimal("100.000000"), usd)

    assert adapter.balance("did:test:requester", usd) == Money(Decimal("100.000000"), usd)
    assert adapter.balance("did:test:provider", usd) == Money.zero(usd)
    assert adapter.get_status(handle) == "released"

    events = ledger.load_all()
    kinds = [e.kind for e in events]
    assert kinds == ["lock", "verdict_issued", "refund_from_verdict"]
    assert events[1].metadata["result"] == "refunded"


# ---------------------------------------------------------------------------
# A5: double-verdict rejected
# ---------------------------------------------------------------------------
def test_double_machine_verdict_rejected(asset_registry, tmp_path: Path):
    """A second machine verdict (non-Tier-3) on the same SLA raises VerdictError."""
    usd = asset_registry.get("mock-usd")
    ledger = SettlementEventLedger(tmp_path)

    node_kp = Ed25519Keypair.generate()
    artifact_bytes = b'{"double_verdict": true}'
    sla = _make_sla_with_hash(artifact_bytes, usd=usd)
    artifact_hash = hashlib.sha256(artifact_bytes).hexdigest()

    verdict1 = OracleVerdict.create(
        sla_id=sla.sla_id,
        artifact_hash=artifact_hash,
        tier=0,
        result="rejected",
        evaluator_did="did:test:oracle",
        evidence={"kind": "schema_fail", "error": "oops"},
        issued_at="2026-04-21T00:00:00Z",
        signer=LocalKeypairSigner(node_kp),
    )
    verdict2 = OracleVerdict.create(
        sla_id=sla.sla_id,
        artifact_hash=artifact_hash,
        tier=0,
        result="accepted",
        evaluator_did="did:test:oracle",
        evidence={"kind": "schema_pass", "detail": "ok"},
        issued_at="2026-04-21T00:01:00Z",
        signer=LocalKeypairSigner(node_kp),
    )

    adapter = MockSettlementAdapter((usd,), ledger=ledger)
    adapter.fund("did:test:requester", Money(Decimal("100.000000"), usd))
    handle = adapter.lock(
        Money(Decimal("100.000000"), usd),
        ref=sla.sla_id,
        nonce="nonce-dbl",
        principal="did:test:requester",
    )

    adapter.release_pending_verdict(
        handle,
        verdict1,
        expected_artifact_hash=artifact_hash,
        requester_did="did:test:requester",
        provider_did="did:test:provider",
    )

    # Fund a new escrow for the second attempt (first is already finalized).
    adapter.fund("did:test:requester", Money(Decimal("100.000000"), usd))
    handle2 = adapter.lock(
        Money(Decimal("100.000000"), usd),
        ref=sla.sla_id,
        nonce="nonce-dbl2",
        principal="did:test:requester",
    )

    with pytest.raises(VerdictError, match="verdict already issued"):
        adapter.release_pending_verdict(
            handle2,
            verdict2,
            expected_artifact_hash=artifact_hash,
            requester_did="did:test:requester",
            provider_did="did:test:provider",
        )


# ---------------------------------------------------------------------------
# A5: founder override path
# ---------------------------------------------------------------------------
def test_founder_override_path_full_sequence(asset_registry, tmp_path: Path):
    """Tier 0 rejected, then Tier 3 founder override accepted.

    Ledger sequence: lock -> verdict_issued(tier0) -> slash_from_verdict
                     -> lock -> verdict_issued(tier3) -> founder_override -> release_from_verdict
    """
    from core.primitives.state import FOUNDER_PRINCIPALS
    usd = asset_registry.get("mock-usd")
    ledger = SettlementEventLedger(tmp_path)

    node_kp = Ed25519Keypair.generate()
    founder_kp = Ed25519Keypair.generate()
    founder_identity = next(iter(FOUNDER_PRINCIPALS))

    artifact_bytes = b'{"override_test": true}'
    sla = _make_sla_with_hash(artifact_bytes, usd=usd)
    artifact_hash = hashlib.sha256(artifact_bytes).hexdigest()

    oracle = Oracle(
        node_did="did:test:oracle",
        node_keypair=node_kp,
        schema_verifier=SchemaVerifier(),
    )

    tier0_verdict = OracleVerdict.create(
        sla_id=sla.sla_id,
        artifact_hash=artifact_hash,
        tier=0,
        result="rejected",
        evaluator_did="did:test:oracle",
        evidence={"kind": "schema_fail", "error": "bad"},
        issued_at="2026-04-21T00:00:00Z",
        signer=LocalKeypairSigner(node_kp),
    )

    # Lock escrow and run tier0 rejection.
    adapter = MockSettlementAdapter((usd,), ledger=ledger)
    adapter.fund("did:test:requester", Money(Decimal("100.000000"), usd))
    handle1 = adapter.lock(
        Money(Decimal("100.000000"), usd),
        ref=sla.sla_id,
        nonce="nonce-fo1",
        principal="did:test:requester",
    )

    adapter.release_pending_verdict(
        handle1,
        tier0_verdict,
        expected_artifact_hash=artifact_hash,
        requester_did="did:test:requester",
        provider_did="did:test:provider",
    )

    # Now do the founder override on a new escrow lock.
    tier3_verdict = oracle.founder_override(
        prior_verdict=tier0_verdict,
        result="accepted",
        reason="requester error confirmed by founder review",
        founder_signer=LocalKeypairSigner(founder_kp),
        founder_identity=founder_identity,
    )

    adapter.fund("did:test:requester", Money(Decimal("100.000000"), usd))
    handle2 = adapter.lock(
        Money(Decimal("100.000000"), usd),
        ref=sla.sla_id,
        nonce="nonce-fo2",
        principal="did:test:requester",
    )

    receipt = adapter.release_pending_verdict(
        handle2,
        tier3_verdict,
        expected_artifact_hash=artifact_hash,
        requester_did="did:test:requester",
        provider_did="did:test:provider",
    )

    assert receipt.outcome == "released"
    assert receipt.to == "did:test:provider"

    events = ledger.load_all()
    kinds = [e.kind for e in events]
    assert kinds == [
        "lock",
        "verdict_issued",       # tier0 rejection
        "slash_from_verdict",
        "lock",
        "verdict_issued",       # tier3 override
        "founder_override",
        "release_from_verdict",
    ], f"unexpected event sequence: {kinds}"

    # tier3 verdict_issued carries overrides metadata.
    tier3_vi = events[4]
    assert tier3_vi.metadata["tier"] == 3
    assert tier3_vi.metadata["overrides"] == tier0_verdict.verdict_hash

    # founder_override event carries identity and reason.
    fo_event = events[5]
    assert fo_event.kind == "founder_override"
    assert fo_event.metadata["founder_identity"] == founder_identity
    assert fo_event.metadata["reason"] != ""


# ---------------------------------------------------------------------------
# A5: error cases
# ---------------------------------------------------------------------------
def test_tampered_verdict_raises_signature_error(asset_registry, tmp_path: Path):
    """Mutating a verdict field after signing raises SignatureError."""
    usd = asset_registry.get("mock-usd")
    ledger = SettlementEventLedger(tmp_path)

    node_kp = Ed25519Keypair.generate()
    artifact_bytes = b'{"tamper_test": true}'
    sla = _make_sla_with_hash(artifact_bytes, usd=usd)
    artifact_hash = hashlib.sha256(artifact_bytes).hexdigest()

    verdict = OracleVerdict.create(
        sla_id=sla.sla_id,
        artifact_hash=artifact_hash,
        tier=0,
        result="accepted",
        evaluator_did="did:test:oracle",
        evidence={"kind": "schema_pass"},
        issued_at="2026-04-21T00:00:00Z",
        signer=LocalKeypairSigner(node_kp),
    )
    # Tamper: swap result field -- OracleVerdict is frozen, so use dataclasses.replace.
    tampered = dataclasses.replace(verdict, result="rejected")

    adapter, handle = _adapter_with_locked_escrow(usd, ledger, sla)

    with pytest.raises(SignatureError):
        adapter.release_pending_verdict(
            handle,
            tampered,
            expected_artifact_hash=artifact_hash,
            requester_did="did:test:requester",
            provider_did="did:test:provider",
        )


def test_mismatched_sla_id_raises_verdict_error(asset_registry, tmp_path: Path):
    """Verdict with wrong sla_id raises VerdictError before any other check."""
    usd = asset_registry.get("mock-usd")
    ledger = SettlementEventLedger(tmp_path)

    node_kp = Ed25519Keypair.generate()
    artifact_bytes = b'{"sla_mismatch": true}'
    sla = _make_sla_with_hash(artifact_bytes, usd=usd)
    artifact_hash = hashlib.sha256(artifact_bytes).hexdigest()

    verdict = OracleVerdict.create(
        sla_id="wrong-sla-id",
        artifact_hash=artifact_hash,
        tier=0,
        result="accepted",
        evaluator_did="did:test:oracle",
        evidence={"kind": "schema_pass"},
        issued_at="2026-04-21T00:00:00Z",
        signer=LocalKeypairSigner(node_kp),
    )

    adapter, handle = _adapter_with_locked_escrow(usd, ledger, sla)

    with pytest.raises(VerdictError, match="sla_id"):
        adapter.release_pending_verdict(
            handle,
            verdict,
            expected_artifact_hash=artifact_hash,
            requester_did="did:test:requester",
            provider_did="did:test:provider",
        )


def test_mismatched_artifact_hash_raises_verdict_error(asset_registry, tmp_path: Path):
    """Verdict artifact_hash != expected_artifact_hash raises VerdictError."""
    usd = asset_registry.get("mock-usd")
    ledger = SettlementEventLedger(tmp_path)

    node_kp = Ed25519Keypair.generate()
    artifact_bytes = b'{"hash_mismatch": true}'
    sla = _make_sla_with_hash(artifact_bytes, usd=usd)
    artifact_hash = hashlib.sha256(artifact_bytes).hexdigest()
    wrong_hash = "b" * 64

    verdict = OracleVerdict.create(
        sla_id=sla.sla_id,
        artifact_hash=wrong_hash,
        tier=0,
        result="accepted",
        evaluator_did="did:test:oracle",
        evidence={"kind": "schema_pass"},
        issued_at="2026-04-21T00:00:00Z",
        signer=LocalKeypairSigner(node_kp),
    )

    adapter, handle = _adapter_with_locked_escrow(usd, ledger, sla)

    with pytest.raises(VerdictError, match="artifact hash mismatch"):
        adapter.release_pending_verdict(
            handle,
            verdict,
            expected_artifact_hash=artifact_hash,
            requester_did="did:test:requester",
            provider_did="did:test:provider",
        )


def test_stablecoin_stub_release_pending_verdict_raises(asset_registry):
    """StablecoinStubAdapter.release_pending_verdict raises NotImplementedError."""
    usd = asset_registry.get("mock-usd")
    stub = StablecoinStubAdapter((usd,), rpc_url="http://localhost", sender_address="0x0")

    with pytest.raises(NotImplementedError, match="stablecoin stub"):
        stub.release_pending_verdict(
            None,  # type: ignore[arg-type]
            None,  # type: ignore[arg-type]
            expected_artifact_hash="x",
            requester_did="r",
            provider_did="p",
        )


# ---------------------------------------------------------------------------
# B3 helpers
# ---------------------------------------------------------------------------
def _make_tier1_verdict(
    sla: InterOrgSLA,
    artifact_bytes: bytes,
    *,
    node_kp: Ed25519Keypair,
    evaluator_did: str = "did:test:evaluator",
    canonical_hash: str = "canonical-hash-abc",
    issued_at: str | None = None,
) -> OracleVerdict:
    """Build a minimal Tier 1 verdict."""
    artifact_hash = hashlib.sha256(artifact_bytes).hexdigest()
    ts = issued_at or "2026-04-21T12:00:00Z"
    return OracleVerdict.create(
        sla_id=sla.sla_id,
        artifact_hash=artifact_hash,
        tier=1,
        result="accepted",
        evaluator_did=evaluator_did,
        evidence={
            "kind": "schema_pass_with_score",
            "evaluator_canonical_hash": canonical_hash,
        },
        issued_at=ts,
        signer=LocalKeypairSigner(node_kp),
        protocol_version="companyos-verdict/0.2",
    )


def _make_sla_for_b3(artifact_bytes: bytes, *, usd: AssetRef) -> InterOrgSLA:
    """Build a minimal SLA for B3 tests with delivery hash set."""
    payment = Money(Decimal("100.000000"), usd)
    sla = InterOrgSLA.create(
        sla_id="test-sla-b3-001",
        requester_node_did="did:test:requester",
        provider_node_did="did:test:provider",
        task_scope="B3 challenge window test",
        deliverable_schema=_valid_schema_envelope(),
        accuracy_requirement=0.9,
        latency_ms=60_000,
        payment=payment,
        penalty_stake=payment,
        nonce=InterOrgSLA.new_nonce(),
        issued_at="2026-04-21T00:00:00Z",
        expires_at="2026-04-28T00:00:00Z",
    )
    artifact_hash = hashlib.sha256(artifact_bytes).hexdigest()
    return sla.with_delivery_hash(artifact_hash)


# ---------------------------------------------------------------------------
# B3: challenge window tests
# ---------------------------------------------------------------------------

def test_tier1_release_before_window_elapses_raises(asset_registry, tmp_path: Path):
    """Tier 1 release before window elapses raises ChallengeWindowError."""
    from datetime import datetime, timezone, timedelta
    from core.primitives.exceptions import ChallengeWindowError

    usd = asset_registry.get("mock-usd")
    ledger = SettlementEventLedger(tmp_path)

    node_kp = Ed25519Keypair.generate()
    artifact_bytes = b'{"summary": "window test"}'
    sla = _make_sla_for_b3(artifact_bytes, usd=usd)

    verdict_issued_at = "2026-04-21T12:00:00Z"
    verdict = _make_tier1_verdict(sla, artifact_bytes, node_kp=node_kp, issued_at=verdict_issued_at)

    adapter, handle = _adapter_with_locked_escrow(usd, ledger, sla)

    # now = 30s after issuance, window = 3600s -> window still open
    fake_now = datetime(2026, 4, 21, 12, 0, 30, tzinfo=timezone.utc)
    with pytest.raises(ChallengeWindowError, match="challenge window still open"):
        adapter.release_pending_verdict(
            handle,
            verdict,
            expected_artifact_hash=sla.artifact_hash_at_delivery,
            requester_did="did:test:requester",
            provider_did="did:test:provider",
            now=fake_now,
            challenge_window_sec=3600,
        )


def test_tier1_release_after_window_elapses_succeeds(asset_registry, tmp_path: Path):
    """Tier 1 release after window elapses with fake now succeeds, emits release_from_verdict."""
    from datetime import datetime, timezone, timedelta
    from core.primitives.exceptions import ChallengeWindowError

    usd = asset_registry.get("mock-usd")
    ledger = SettlementEventLedger(tmp_path)

    node_kp = Ed25519Keypair.generate()
    artifact_bytes = b'{"summary": "after window"}'
    sla = _make_sla_for_b3(artifact_bytes, usd=usd)

    verdict_issued_at = "2026-04-21T12:00:00Z"
    verdict = _make_tier1_verdict(sla, artifact_bytes, node_kp=node_kp, issued_at=verdict_issued_at)

    adapter, handle = _adapter_with_locked_escrow(usd, ledger, sla)

    # now = 4000s after issuance, window = 3600s -> window elapsed
    fake_now = datetime(2026, 4, 21, 13, 6, 40, tzinfo=timezone.utc)  # 12:00 + 4000s
    receipt = adapter.release_pending_verdict(
        handle,
        verdict,
        expected_artifact_hash=sla.artifact_hash_at_delivery,
        requester_did="did:test:requester",
        provider_did="did:test:provider",
        now=fake_now,
        challenge_window_sec=3600,
    )

    assert receipt.outcome == "released"
    events = ledger.load_all()
    kinds = [e.kind for e in events]
    assert "release_from_verdict" in kinds


def test_raise_challenge_blocks_release(asset_registry, tmp_path: Path):
    """raise_challenge emits challenge_raised; subsequent release raises ChallengeWindowError."""
    from datetime import datetime, timezone
    from core.primitives.exceptions import ChallengeWindowError
    from core.primitives.challenge import Challenge
    from core.primitives.signer import LocalKeypairSigner

    usd = asset_registry.get("mock-usd")
    ledger = SettlementEventLedger(tmp_path)

    node_kp = Ed25519Keypair.generate()
    challenger_kp = Ed25519Keypair.generate()
    artifact_bytes = b'{"summary": "challenge blocks"}'
    sla = _make_sla_for_b3(artifact_bytes, usd=usd)

    # Use a verdict issued far in the future so Challenge.create()'s real-time
    # issued_at is always within the challenge window.
    verdict_issued_at = "2099-01-01T00:00:00Z"
    large_window = 9_999_999  # ~115 days from 2099-01-01 -> well past any real-time challenge
    verdict = _make_tier1_verdict(sla, artifact_bytes, node_kp=node_kp, issued_at=verdict_issued_at)

    adapter, handle = _adapter_with_locked_escrow(usd, ledger, sla)

    # Raise a challenge within the window.
    challenge = Challenge.create(
        prior_verdict=verdict,
        challenger_did="did:test:requester",
        reason="I dispute this verdict",
        signer=LocalKeypairSigner(challenger_kp),
    )
    adapter.raise_challenge(
        handle,
        challenge,
        requester_did="did:test:requester",
        provider_did="did:test:provider",
        prior_verdict=verdict,
        challenge_window_sec=large_window,
    )

    # Verify challenge_raised was emitted.
    events_after_challenge = ledger.load_all()
    kinds = [e.kind for e in events_after_challenge]
    assert "challenge_raised" in kinds

    # Now try to release (even with now past the window) -> unresolved challenge blocks it.
    future_now = datetime(2026, 4, 21, 14, 0, 0, tzinfo=timezone.utc)
    with pytest.raises(ChallengeWindowError, match="unresolved challenge"):
        adapter.release_pending_verdict(
            handle,
            verdict,
            expected_artifact_hash=sla.artifact_hash_at_delivery,
            requester_did="did:test:requester",
            provider_did="did:test:provider",
            now=future_now,
            challenge_window_sec=large_window,
        )


def test_tier3_supersedes_challenge(asset_registry, tmp_path: Path):
    """Tier 3 verdict with evidence.challenge_hash emits challenge_resolved + release sequence."""
    from core.primitives.challenge import Challenge
    from core.primitives.signer import LocalKeypairSigner
    from core.primitives.state import FOUNDER_PRINCIPALS

    usd = asset_registry.get("mock-usd")
    ledger = SettlementEventLedger(tmp_path)

    node_kp = Ed25519Keypair.generate()
    founder_kp = Ed25519Keypair.generate()
    challenger_kp = Ed25519Keypair.generate()
    founder_identity = next(iter(FOUNDER_PRINCIPALS))

    artifact_bytes = b'{"summary": "tier3 challenge supersede"}'
    sla = _make_sla_for_b3(artifact_bytes, usd=usd)

    artifact_hash = hashlib.sha256(artifact_bytes).hexdigest()
    # Verdict issued far in the future so any real-time Challenge.create() issued_at
    # is always before the window closes (9999999s = ~115 days from 2099-01-01).
    tier1_verdict = _make_tier1_verdict(
        sla, artifact_bytes, node_kp=node_kp, issued_at="2099-01-01T00:00:00Z"
    )
    large_window = 9_999_999

    adapter, handle = _adapter_with_locked_escrow(usd, ledger, sla)

    # Raise a challenge.
    challenge = Challenge.create(
        prior_verdict=tier1_verdict,
        challenger_did="did:test:requester",
        reason="Disputing tier1 result",
        signer=LocalKeypairSigner(challenger_kp),
    )
    adapter.raise_challenge(
        handle,
        challenge,
        requester_did="did:test:requester",
        provider_did="did:test:provider",
        prior_verdict=tier1_verdict,
        challenge_window_sec=large_window,
    )

    # Build a Tier 3 override that references both overrides and challenge_hash.
    # Use OracleVerdict.create directly so the signature is valid with the full evidence.
    tier3_verdict = OracleVerdict.create(
        sla_id=sla.sla_id,
        artifact_hash=artifact_hash,
        tier=3,
        result="accepted",
        evaluator_did="did:test:oracle",
        evidence={
            "kind": "founder_override",
            "overrides": tier1_verdict.verdict_hash,
            "reason": "Founder reviewed and accepts",
            "founder_identity": founder_identity,
            "challenge_hash": challenge.challenge_hash,
        },
        issued_at="2026-04-21T13:00:00Z",
        signer=LocalKeypairSigner(founder_kp),
    )

    receipt = adapter.release_pending_verdict(
        handle,
        tier3_verdict,
        expected_artifact_hash=sla.artifact_hash_at_delivery,
        requester_did="did:test:requester",
        provider_did="did:test:provider",
    )

    assert receipt.outcome == "released"
    events = ledger.load_all()
    kinds = [e.kind for e in events]
    assert "challenge_resolved" in kinds
    assert "release_from_verdict" in kinds
    # founder_override -> challenge_resolved -> release_from_verdict
    assert kinds.index("founder_override") < kinds.index("challenge_resolved")
    assert kinds.index("challenge_resolved") < kinds.index("release_from_verdict")


def test_challenge_from_non_counterparty_raises(asset_registry, tmp_path: Path):
    """raise_challenge from non-counterparty DID raises VerdictError."""
    from core.primitives.exceptions import VerdictError
    from core.primitives.challenge import Challenge
    from core.primitives.signer import LocalKeypairSigner

    usd = asset_registry.get("mock-usd")
    ledger = SettlementEventLedger(tmp_path)

    node_kp = Ed25519Keypair.generate()
    outsider_kp = Ed25519Keypair.generate()
    artifact_bytes = b'{"summary": "non-counterparty challenge"}'
    sla = _make_sla_for_b3(artifact_bytes, usd=usd)

    verdict = _make_tier1_verdict(sla, artifact_bytes, node_kp=node_kp)

    adapter, handle = _adapter_with_locked_escrow(usd, ledger, sla)

    # Create challenge from an outsider (not requester or provider).
    challenge = Challenge.create(
        prior_verdict=verdict,
        challenger_did="did:test:outsider",
        reason="I am not a party",
        signer=LocalKeypairSigner(outsider_kp),
    )
    with pytest.raises(VerdictError, match="unauthorized challenger"):
        adapter.raise_challenge(
            handle,
            challenge,
            requester_did="did:test:requester",
            provider_did="did:test:provider",
            prior_verdict=verdict,
            challenge_window_sec=3600,
        )


def test_late_challenge_ruling16_raises(asset_registry, tmp_path: Path):
    """raise_challenge with issued_at past the window raises ChallengeWindowError (Ruling 16).

    The verdict is issued in the far past so that Challenge.create()'s real-time
    issued_at is always beyond the challenge window (Ruling 16 violation). The
    challenge signature is valid; only the Ruling 16 time check fails.
    """
    from core.primitives.exceptions import ChallengeWindowError
    from core.primitives.challenge import Challenge
    from core.primitives.signer import LocalKeypairSigner

    usd = asset_registry.get("mock-usd")
    ledger = SettlementEventLedger(tmp_path)

    node_kp = Ed25519Keypair.generate()
    challenger_kp = Ed25519Keypair.generate()
    artifact_bytes = b'{"summary": "late challenge"}'
    sla = _make_sla_for_b3(artifact_bytes, usd=usd)

    # Verdict issued in the far past; challenge_window_sec=3600 means window
    # closed long before Challenge.create()'s real-time issued_at.
    verdict_issued_at = "2020-01-01T00:00:00Z"
    short_window = 3600
    verdict = _make_tier1_verdict(sla, artifact_bytes, node_kp=node_kp, issued_at=verdict_issued_at)

    adapter, handle = _adapter_with_locked_escrow(usd, ledger, sla)

    # Create a challenge now (real time, well past the window).
    # verify_signature() will pass (challenge is freshly signed), but the
    # Ruling 16 time check will fail because challenge.issued_at > window_end.
    challenge = Challenge.create(
        prior_verdict=verdict,
        challenger_did="did:test:requester",
        reason="Late challenge past the window",
        signer=LocalKeypairSigner(challenger_kp),
    )

    with pytest.raises(ChallengeWindowError, match="challenge issued after window elapsed"):
        adapter.raise_challenge(
            handle,
            challenge,
            requester_did="did:test:requester",
            provider_did="did:test:provider",
            prior_verdict=verdict,
            challenge_window_sec=short_window,
        )


def test_tier3_challenge_preemption_full_sequence(asset_registry, tmp_path: Path):
    """Full sequence: Tier 1 -> challenge_raised -> Tier 3 override -> challenge_resolved -> release.

    Ledger sequence must be:
    lock -> verdict_issued(t=1) -> challenge_raised -> verdict_issued(t=3)
         -> founder_override -> challenge_resolved -> release_from_verdict

    The Tier 1 release attempt emits verdict_issued then raises ChallengeWindowError
    (window still open). The challenge is then raised, and the founder issues a Tier 3
    override that resolves both the challenge and the settlement.
    """
    from datetime import datetime, timezone
    from core.primitives.challenge import Challenge
    from core.primitives.exceptions import ChallengeWindowError
    from core.primitives.signer import LocalKeypairSigner
    from core.primitives.state import FOUNDER_PRINCIPALS

    usd = asset_registry.get("mock-usd")
    ledger = SettlementEventLedger(tmp_path)

    node_kp = Ed25519Keypair.generate()
    founder_kp = Ed25519Keypair.generate()
    challenger_kp = Ed25519Keypair.generate()
    founder_identity = next(iter(FOUNDER_PRINCIPALS))

    artifact_bytes = b'{"summary": "full preemption test"}'
    sla = _make_sla_for_b3(artifact_bytes, usd=usd)

    # Verdict issued far in the future; large window so Challenge.create()'s real-time
    # issued_at is always within the window. inside_window_now is before the verdict
    # issued_at, which means the window hasn't even started -- "challenge window still open"
    # because now (2026-04-21) < verdict_expires_at (2099-01-01 + 9999999s).
    verdict_issued_at = "2099-01-01T00:00:00Z"
    large_window = 9_999_999
    tier1_verdict = _make_tier1_verdict(
        sla, artifact_bytes, node_kp=node_kp, issued_at=verdict_issued_at
    )

    adapter, handle = _adapter_with_locked_escrow(usd, ledger, sla)

    # Step 1: attempt release with fake now well before verdict_expires_at ->
    # emits verdict_issued(t=1) then raises ChallengeWindowError.
    inside_window_now = datetime(2026, 4, 21, 12, 30, 0, tzinfo=timezone.utc)
    with pytest.raises(ChallengeWindowError, match="challenge window still open"):
        adapter.release_pending_verdict(
            handle,
            tier1_verdict,
            expected_artifact_hash=sla.artifact_hash_at_delivery,
            requester_did="did:test:requester",
            provider_did="did:test:provider",
            now=inside_window_now,
            challenge_window_sec=large_window,
        )

    # Step 2: raise_challenge -> challenge_raised.
    challenge = Challenge.create(
        prior_verdict=tier1_verdict,
        challenger_did="did:test:requester",
        reason="Full sequence test challenge",
        signer=LocalKeypairSigner(challenger_kp),
    )
    adapter.raise_challenge(
        handle,
        challenge,
        requester_did="did:test:requester",
        provider_did="did:test:provider",
        prior_verdict=tier1_verdict,
        challenge_window_sec=large_window,
    )

    # Step 3: Founder issues Tier 3 override with both overrides and challenge_hash.
    # Build directly with OracleVerdict.create so the signature is valid with full evidence.
    tier3_verdict = OracleVerdict.create(
        sla_id=sla.sla_id,
        artifact_hash=hashlib.sha256(artifact_bytes).hexdigest(),
        tier=3,
        result="accepted",
        evaluator_did="did:test:oracle",
        evidence={
            "kind": "founder_override",
            "overrides": tier1_verdict.verdict_hash,
            "reason": "Founder overrides challenge",
            "founder_identity": founder_identity,
            "challenge_hash": challenge.challenge_hash,
        },
        issued_at="2026-04-21T14:00:00Z",
        signer=LocalKeypairSigner(founder_kp),
    )

    # Step 4: release_pending_verdict with Tier 3 (bypasses window check, resolves challenge).
    receipt = adapter.release_pending_verdict(
        handle,
        tier3_verdict,
        expected_artifact_hash=sla.artifact_hash_at_delivery,
        requester_did="did:test:requester",
        provider_did="did:test:provider",
    )

    assert receipt.outcome == "released"
    assert receipt.to == "did:test:provider"

    events = ledger.load_all()
    kinds = [e.kind for e in events]

    expected_sequence = [
        "lock",
        "verdict_issued",       # tier=1 (emitted before ChallengeWindowError raised)
        "challenge_raised",
        "verdict_issued",       # tier=3
        "founder_override",
        "challenge_resolved",
        "release_from_verdict",
    ]
    assert kinds == expected_sequence, f"unexpected event sequence: {kinds}"

    # Verify tier metadata.
    tier1_vi = events[1]
    assert tier1_vi.metadata["tier"] == 1

    tier3_vi = events[3]
    assert tier3_vi.metadata["tier"] == 3
    assert tier3_vi.metadata["overrides"] == tier1_verdict.verdict_hash

    # challenge_resolved references the challenge.
    cr_ev = events[5]
    assert cr_ev.kind == "challenge_resolved"
    assert cr_ev.metadata["challenge_hash"] == challenge.challenge_hash


def test_evaluator_did_mismatch_raises(asset_registry, tmp_path: Path):
    """Tier 1 release with wrong evaluator DID raises EvaluatorAuthorizationError."""
    from datetime import datetime, timezone
    from core.primitives.exceptions import EvaluatorAuthorizationError

    usd = asset_registry.get("mock-usd")
    ledger = SettlementEventLedger(tmp_path)

    node_kp = Ed25519Keypair.generate()
    artifact_bytes = b'{"summary": "evaluator did mismatch"}'
    sla = _make_sla_for_b3(artifact_bytes, usd=usd)

    verdict = _make_tier1_verdict(
        sla, artifact_bytes, node_kp=node_kp, evaluator_did="did:test:actual-evaluator"
    )

    adapter, handle = _adapter_with_locked_escrow(usd, ledger, sla)

    future_now = datetime(2026, 4, 22, 12, 0, 0, tzinfo=timezone.utc)
    with pytest.raises(EvaluatorAuthorizationError, match="evaluator DID mismatch"):
        adapter.release_pending_verdict(
            handle,
            verdict,
            expected_artifact_hash=sla.artifact_hash_at_delivery,
            requester_did="did:test:requester",
            provider_did="did:test:provider",
            now=future_now,
            challenge_window_sec=3600,
            expected_primary_evaluator_did="did:test:wrong-evaluator",
        )


def test_evaluator_canonical_hash_drift_raises(asset_registry, tmp_path: Path):
    """Tier 1 release with wrong canonical hash raises EvaluatorAuthorizationError."""
    from datetime import datetime, timezone
    from core.primitives.exceptions import EvaluatorAuthorizationError

    usd = asset_registry.get("mock-usd")
    ledger = SettlementEventLedger(tmp_path)

    node_kp = Ed25519Keypair.generate()
    artifact_bytes = b'{"summary": "hash drift"}'
    sla = _make_sla_for_b3(artifact_bytes, usd=usd)

    verdict = _make_tier1_verdict(
        sla, artifact_bytes, node_kp=node_kp, canonical_hash="actual-hash-xyz"
    )

    adapter, handle = _adapter_with_locked_escrow(usd, ledger, sla)

    future_now = datetime(2026, 4, 22, 12, 0, 0, tzinfo=timezone.utc)
    with pytest.raises(EvaluatorAuthorizationError, match="evaluator canonical hash drift"):
        adapter.release_pending_verdict(
            handle,
            verdict,
            expected_artifact_hash=sla.artifact_hash_at_delivery,
            requester_did="did:test:requester",
            provider_did="did:test:provider",
            now=future_now,
            challenge_window_sec=3600,
            expected_evaluator_canonical_hash="expected-hash-different",
        )


def test_tier0_release_with_now_kwarg_ignored(asset_registry, tmp_path: Path):
    """Tier 0 verdict: now kwarg is ignored, releases immediately (backward compat)."""
    from datetime import datetime, timezone

    usd = asset_registry.get("mock-usd")
    ledger = SettlementEventLedger(tmp_path)

    node_kp = Ed25519Keypair.generate()
    artifact_bytes = b'{"summary": "tier0 now ignored"}'
    sla = _make_sla_for_b3(artifact_bytes, usd=usd)

    oracle = Oracle(
        node_did="did:test:oracle",
        node_keypair=node_kp,
        schema_verifier=SchemaVerifier(),
    )
    verdict = oracle.evaluate_tier0(sla, artifact_bytes)
    assert verdict.tier == 0

    adapter, handle = _adapter_with_locked_escrow(usd, ledger, sla)

    # Pass a very old "now" that would block a Tier 1 verdict but Tier 0 ignores it.
    old_now = datetime(2020, 1, 1, tzinfo=timezone.utc)
    receipt = adapter.release_pending_verdict(
        handle,
        verdict,
        expected_artifact_hash=sla.artifact_hash_at_delivery,
        requester_did="did:test:requester",
        provider_did="did:test:provider",
        now=old_now,
        challenge_window_sec=3600,
    )

    assert receipt.outcome == "released"
    events = ledger.load_all()
    kinds = [e.kind for e in events]
    assert kinds == ["lock", "verdict_issued", "release_from_verdict"]


def test_tier3_without_challenge_hash_no_challenge_resolved_event(
    asset_registry, tmp_path: Path
):
    """Tier 3 verdict without challenge_hash: no challenge_resolved event emitted."""
    from core.primitives.state import FOUNDER_PRINCIPALS
    from core.primitives.schema_verifier import SchemaVerifier

    usd = asset_registry.get("mock-usd")
    ledger = SettlementEventLedger(tmp_path)

    node_kp = Ed25519Keypair.generate()
    founder_kp = Ed25519Keypair.generate()
    founder_identity = next(iter(FOUNDER_PRINCIPALS))

    artifact_bytes = b'{"summary": "tier3 no challenge_hash"}'
    sla = _make_sla_for_b3(artifact_bytes, usd=usd)
    artifact_hash = hashlib.sha256(artifact_bytes).hexdigest()

    # First, issue a Tier 0 rejection so the Tier 3 override is valid.
    tier0_verdict = OracleVerdict.create(
        sla_id=sla.sla_id,
        artifact_hash=artifact_hash,
        tier=0,
        result="rejected",
        evaluator_did="did:test:oracle",
        evidence={"kind": "schema_fail", "error": "bad"},
        issued_at="2026-04-21T00:00:00Z",
        signer=LocalKeypairSigner(node_kp),
    )

    adapter = MockSettlementAdapter((usd,), ledger=ledger)
    adapter.fund("did:test:requester", Money(Decimal("100.000000"), usd))
    handle1 = adapter.lock(
        Money(Decimal("100.000000"), usd),
        ref=sla.sla_id,
        nonce="nonce-t3-nch1",
        principal="did:test:requester",
    )
    adapter.release_pending_verdict(
        handle1,
        tier0_verdict,
        expected_artifact_hash=artifact_hash,
        requester_did="did:test:requester",
        provider_did="did:test:provider",
    )

    oracle = Oracle(
        node_did="did:test:oracle",
        node_keypair=node_kp,
        schema_verifier=SchemaVerifier(),
    )
    tier3_verdict = oracle.founder_override(
        prior_verdict=tier0_verdict,
        result="accepted",
        reason="No challenge involved",
        founder_signer=LocalKeypairSigner(founder_kp),
        founder_identity=founder_identity,
    )
    # No challenge_hash in evidence.
    assert "challenge_hash" not in tier3_verdict.evidence

    adapter.fund("did:test:requester", Money(Decimal("100.000000"), usd))
    handle2 = adapter.lock(
        Money(Decimal("100.000000"), usd),
        ref=sla.sla_id,
        nonce="nonce-t3-nch2",
        principal="did:test:requester",
    )
    receipt = adapter.release_pending_verdict(
        handle2,
        tier3_verdict,
        expected_artifact_hash=artifact_hash,
        requester_did="did:test:requester",
        provider_did="did:test:provider",
    )

    assert receipt.outcome == "released"
    events = ledger.load_all()
    kinds = [e.kind for e in events]
    assert "challenge_resolved" not in kinds
    assert "founder_override" in kinds
    assert "release_from_verdict" in kinds
