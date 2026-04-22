"""
core/primitives/settlement_adapters/stablecoin_stub.py — chain-shaped stub
===========================================================================

Ticket 4 of the v0 Currency-Agnostic Settlement Architecture.

`StablecoinStubAdapter` proves the `SettlementAdapter` Protocol survives
a realistic real-chain implementation without schema change. It declares
the capability surface a future EVM-style adapter would expose — a
tuple of supported assets, an RPC endpoint, and a sender address — but
intentionally leaves every network op (`lock`, `release`, `slash`,
`balance`, `get_status`) as `NotImplementedError`.

The value of this stub is entirely structural: at import + construction
time it must satisfy the Protocol (runtime-checkable), register cleanly
in an `AdapterRegistry`, and route by asset just like the mock. That
guarantees Ticket 1's asset model + Ticket 3's registry are not
accidentally overfit to the in-memory mock.

Out of scope for v0: wallet signing, RPC calls, gas estimation, tx
submission, receipt decoding, reorg handling. Ticket for a real
implementation comes later.
"""
from __future__ import annotations

from typing import Any

from core.primitives.asset import AssetRef
from core.primitives.money import Money
from core.primitives.settlement_adapters.base import (
    EscrowHandle,
    EscrowStatus,
    SettlementReceipt,
)


_NOT_IMPL_MSG = "stablecoin adapter v0: network ops out of scope"


class StablecoinStubAdapter:
    """Chain-shaped stub adapter. Structurally implements `SettlementAdapter`.

    Construct with the tuple of supported assets plus the two
    connection-shaped parameters a real EVM adapter would need. The
    adapter stores them for future use but never touches the network
    in v0.

    `supports(asset)` is the only functional method; every other
    Protocol method raises `NotImplementedError` with a consistent
    message.
    """

    def __init__(
        self,
        supported_assets: tuple[AssetRef, ...],
        rpc_url: str,
        sender_address: str,
    ) -> None:
        if not supported_assets:
            raise ValueError(
                "StablecoinStubAdapter requires at least one supported AssetRef"
            )
        self._supported_ids: set[str] = {a.asset_id for a in supported_assets}
        self.supported_assets: tuple[AssetRef, ...] = supported_assets
        # Stored but unused in v0 — a real adapter would use these to
        # build signed transactions against an RPC endpoint.
        self.rpc_url: str = rpc_url
        self.sender_address: str = sender_address

    # ------------------------------------------------------------------
    # Capability
    # ------------------------------------------------------------------
    def supports(self, asset: AssetRef) -> bool:
        return asset.asset_id in self._supported_ids

    # ------------------------------------------------------------------
    # Network ops — all deliberately unimplemented in v0.
    # ------------------------------------------------------------------
    def lock(
        self,
        amount: Money,
        ref: str,
        *,
        nonce: str,
    ) -> EscrowHandle:
        raise NotImplementedError(_NOT_IMPL_MSG)

    def release(self, handle: EscrowHandle, to: str) -> SettlementReceipt:
        raise NotImplementedError(_NOT_IMPL_MSG)

    def slash(
        self,
        handle: EscrowHandle,
        percent: int,
        beneficiary: str | None,
    ) -> SettlementReceipt:
        raise NotImplementedError(_NOT_IMPL_MSG)

    def balance(self, principal: str, asset: AssetRef) -> Money:
        raise NotImplementedError(_NOT_IMPL_MSG)

    def get_status(self, handle: EscrowHandle) -> EscrowStatus:
        raise NotImplementedError(_NOT_IMPL_MSG)

    def release_pending_verdict(
        self,
        handle: EscrowHandle,
        verdict: Any,
        *,
        expected_artifact_hash: str,
        requester_did: str,
        provider_did: str,
        now: Any = None,
        challenge_window_sec: Any = None,
        expected_primary_evaluator_did: Any = None,
        expected_evaluator_canonical_hash: Any = None,
    ) -> SettlementReceipt:
        raise NotImplementedError(
            "release_pending_verdict not implemented on stablecoin stub"
        )

    def raise_challenge(
        self,
        handle: EscrowHandle,
        challenge: Any,
        *,
        requester_did: str,
        provider_did: str,
        prior_verdict: Any,
        challenge_window_sec: int,
    ) -> None:
        raise NotImplementedError(_NOT_IMPL_MSG)


__all__ = ["StablecoinStubAdapter"]
