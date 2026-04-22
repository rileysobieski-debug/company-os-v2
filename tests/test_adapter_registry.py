"""
tests/test_adapter_registry.py — Ticket 3 coverage
==================================================
Tests for `core.primitives.settlement_adapters.base.AdapterRegistry`
and the `SettlementAdapter` Protocol.

Covered:
- successful registration, adapter_for returns correct adapter
- UnsupportedAssetError when no adapter claims
- two adapters with disjoint asset sets coexist
- one adapter can support multiple assets (multi-asset forward compat)
- AdapterConflictError when second adapter overlaps with first
- SettlementAdapter is runtime_checkable (isinstance works)
"""
from __future__ import annotations

from decimal import Decimal
from typing import Iterable

import pytest

from core.primitives.asset import AssetRef, AssetRegistry
from core.primitives.exceptions import (
    AdapterConflictError,
    UnsupportedAssetError,
)
from core.primitives.money import Money
from core.primitives.settlement_adapters import (
    AdapterRegistry,
    EscrowHandle,
    EscrowHandleId,
    EscrowStatus,
    SettlementAdapter,
    SettlementReceipt,
)
from core.primitives.settlement_adapters.mock_adapter import MockSettlementAdapter


# ---------------------------------------------------------------------------
# Test helpers: minimal stub adapters that only implement capability.
# ---------------------------------------------------------------------------
class _SingleAssetStub:
    """Minimal adapter stub that supports exactly one asset_id."""

    def __init__(self, asset_id: str) -> None:
        self._asset_id = asset_id

    def supports(self, asset: AssetRef) -> bool:
        return asset.asset_id == self._asset_id

    # The registry only needs `supports()`; these stubs are never locked
    # against in this test file. Methods below satisfy the Protocol for
    # isinstance checks but are never invoked.
    def lock(self, amount, ref, *, nonce):  # pragma: no cover
        raise NotImplementedError

    def release(self, handle, to):  # pragma: no cover
        raise NotImplementedError

    def slash(self, handle, percent, beneficiary):  # pragma: no cover
        raise NotImplementedError

    def balance(self, principal, asset):  # pragma: no cover
        raise NotImplementedError

    def get_status(self, handle):  # pragma: no cover
        raise NotImplementedError

    def release_pending_verdict(  # pragma: no cover
        self, handle, verdict, *, expected_artifact_hash, requester_did, provider_did,
        now=None, challenge_window_sec=None,
        expected_primary_evaluator_did=None, expected_evaluator_canonical_hash=None,
    ):
        raise NotImplementedError

    def raise_challenge(  # pragma: no cover
        self, handle, challenge, *, requester_did, provider_did,
        prior_verdict, challenge_window_sec,
    ):
        raise NotImplementedError


class _MultiAssetStub:
    """Adapter stub that supports several asset_ids — multi-asset case."""

    def __init__(self, asset_ids: Iterable[str]) -> None:
        self._asset_ids = set(asset_ids)

    def supports(self, asset: AssetRef) -> bool:
        return asset.asset_id in self._asset_ids

    def lock(self, amount, ref, *, nonce):  # pragma: no cover
        raise NotImplementedError

    def release(self, handle, to):  # pragma: no cover
        raise NotImplementedError

    def slash(self, handle, percent, beneficiary):  # pragma: no cover
        raise NotImplementedError

    def balance(self, principal, asset):  # pragma: no cover
        raise NotImplementedError

    def get_status(self, handle):  # pragma: no cover
        raise NotImplementedError

    def release_pending_verdict(  # pragma: no cover
        self, handle, verdict, *, expected_artifact_hash, requester_did, provider_did,
        now=None, challenge_window_sec=None,
        expected_primary_evaluator_did=None, expected_evaluator_canonical_hash=None,
    ):
        raise NotImplementedError

    def raise_challenge(  # pragma: no cover
        self, handle, challenge, *, requester_did, provider_did,
        prior_verdict, challenge_window_sec,
    ):
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Successful registration + dispatch
# ---------------------------------------------------------------------------
def test_registered_adapter_dispatches_for_its_asset(asset_registry):
    reg = AdapterRegistry(asset_registry)
    usd_adapter = _SingleAssetStub("mock-usd")
    reg.register(usd_adapter)

    usd = asset_registry.get("mock-usd")
    assert reg.adapter_for(usd) is usd_adapter


def test_unsupported_asset_raises_when_no_adapter_claims(asset_registry):
    reg = AdapterRegistry(asset_registry)
    # Register an adapter only for EUR.
    reg.register(_SingleAssetStub("mock-eur"))

    usd = asset_registry.get("mock-usd")
    with pytest.raises(UnsupportedAssetError) as excinfo:
        reg.adapter_for(usd)
    assert "mock-usd" in str(excinfo.value)


def test_empty_registry_raises_unsupported_for_any_asset(asset_registry):
    reg = AdapterRegistry(asset_registry)
    usd = asset_registry.get("mock-usd")
    with pytest.raises(UnsupportedAssetError):
        reg.adapter_for(usd)


# ---------------------------------------------------------------------------
# Disjoint coexistence + multi-asset forward compat
# ---------------------------------------------------------------------------
def test_two_disjoint_adapters_coexist(asset_registry):
    reg = AdapterRegistry(asset_registry)
    usd_adapter = _SingleAssetStub("mock-usd")
    eur_adapter = _SingleAssetStub("mock-eur")
    reg.register(usd_adapter)
    reg.register(eur_adapter)

    usd = asset_registry.get("mock-usd")
    eur = asset_registry.get("mock-eur")

    assert reg.adapter_for(usd) is usd_adapter
    assert reg.adapter_for(eur) is eur_adapter


def test_one_adapter_supporting_multiple_assets(asset_registry):
    """Multi-asset forward-compat: a single adapter may claim several assets.

    This is the key test for the EVM-adapter future — USDC, DAI, ETH
    on the same chain all handled by one adapter.
    """
    reg = AdapterRegistry(asset_registry)
    evm_adapter = _MultiAssetStub(["mock-usd", "mock-eur", "usdc-base"])
    reg.register(evm_adapter)

    for aid in ("mock-usd", "mock-eur", "usdc-base"):
        asset = asset_registry.get(aid)
        assert reg.adapter_for(asset) is evm_adapter


# ---------------------------------------------------------------------------
# Conflict detection
# ---------------------------------------------------------------------------
def test_adapter_conflict_raised_on_overlap(asset_registry):
    reg = AdapterRegistry(asset_registry)
    first = _SingleAssetStub("mock-usd")
    reg.register(first)

    second = _SingleAssetStub("mock-usd")
    with pytest.raises(AdapterConflictError) as excinfo:
        reg.register(second)
    assert "mock-usd" in str(excinfo.value)


def test_adapter_conflict_partial_overlap_raises(asset_registry):
    """Second adapter overlaps on a subset of assets -> still a conflict."""
    reg = AdapterRegistry(asset_registry)
    first = _MultiAssetStub(["mock-usd", "mock-eur"])
    reg.register(first)

    # Overlaps on mock-eur only.
    second = _MultiAssetStub(["mock-eur", "usdc-base"])
    with pytest.raises(AdapterConflictError) as excinfo:
        reg.register(second)
    assert "mock-eur" in str(excinfo.value)


def test_conflict_check_only_against_known_assets(tmp_path):
    """Adapters that claim assets unknown to the registry don't conflict.

    Overlap is checked against `asset_registry.ids()`. If the registry
    has only mock-usd, and two adapters both support mock-eur (unknown),
    they can coexist. This keeps the conflict check tractable.
    """
    (tmp_path / "usd.yaml").write_text(
        "asset_id: mock-usd\ncontract: USD\ndecimals: 6\n",
        encoding="utf-8",
    )
    asset_reg = AssetRegistry()
    asset_reg.load(tmp_path)

    reg = AdapterRegistry(asset_reg)
    a = _SingleAssetStub("mock-eur")  # not known to asset_reg
    b = _SingleAssetStub("mock-eur")  # also not known
    reg.register(a)
    reg.register(b)  # must not raise


# ---------------------------------------------------------------------------
# Protocol behavior
# ---------------------------------------------------------------------------
def test_settlement_adapter_protocol_is_runtime_checkable(asset_registry):
    """MockSettlementAdapter should satisfy isinstance(..., SettlementAdapter)."""
    usd = asset_registry.get("mock-usd")
    adapter = MockSettlementAdapter((usd,))
    assert isinstance(adapter, SettlementAdapter)


def test_stub_satisfies_protocol_structurally(asset_registry):
    """Structural typing: the stubs here also satisfy the Protocol."""
    assert isinstance(_SingleAssetStub("mock-usd"), SettlementAdapter)
    assert isinstance(_MultiAssetStub(["a", "b"]), SettlementAdapter)


def test_callers_dispatch_through_registry(asset_registry):
    """End-to-end: caller resolves adapter via registry, then invokes lock."""
    usd = asset_registry.get("mock-usd")
    reg = AdapterRegistry(asset_registry)
    adapter = MockSettlementAdapter((usd,))
    reg.register(adapter)

    adapter.fund("alice", Money(Decimal("100"), usd))
    resolved = reg.adapter_for(usd)
    handle = resolved.lock(
        Money(Decimal("25"), usd),
        ref="sla-1",
        nonce="n1",
        principal="alice",
    )
    assert resolved.get_status(handle) == "locked"
