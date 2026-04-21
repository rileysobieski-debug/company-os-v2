"""
core/primitives/settlement_adapters/mock_adapter.py — in-memory settlement
==========================================================================

Ticket 3 of the v0 Currency-Agnostic Settlement Architecture.

`MockSettlementAdapter` is a pure-Python, single-process, in-memory
adapter used by tests and the scenario simulator. It conforms to the
`SettlementAdapter` protocol from `base.py` with two deliberate
extensions for mock-only use:

1. `fund(principal, amount)` — credits a principal's balance out of
   thin air. Real adapters infer balances from on-chain state; the mock
   needs an explicit seed path so tests can set up initial positions.
2. `lock(..., *, principal: str)` — adds a keyword-only `principal`
   parameter the Protocol does not require. Real adapters infer the
   locker from wallet context (msg.sender, session key, etc.); the mock
   has no wallet, so the caller names the locker explicitly. The
   scenario ledger (Ticket 6) will wire this.

Replay resistance: every `lock` must carry a nonce. The adapter tracks
consumed nonces in `_consumed_nonces` and rejects any reuse with
`EscrowStateError("nonce replay detected")`, even if other fields
differ. Nonces are never removed — this is append-only.

The optional `ledger=None` constructor kwarg is forward-compat for
Ticket 9's `SettlementEventLedger`. It is stored but not used; the
import of the ledger class intentionally lives in Ticket 9 to avoid
reverse-dependency.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from core.primitives.asset import AssetRef
from core.primitives.exceptions import (
    EscrowStateError,
    UnsupportedAssetError,
    VerdictError,
)
from core.primitives.money import Money
from core.primitives.settlement_adapters.base import (
    EscrowHandle,
    EscrowHandleId,
    EscrowStatus,
    SettlementReceipt,
)


# ---------------------------------------------------------------------------
# Internal escrow record
# ---------------------------------------------------------------------------
@dataclass
class _EscrowRecord:
    """Per-escrow state the mock adapter maintains.

    Callers must never touch this — exposed only for internal bookkeeping
    within the adapter. The adapter surfaces state exclusively through
    `get_status`, `balance`, and `SettlementReceipt` return values.
    """

    handle: EscrowHandle
    locker: str                  # principal who funded the lock
    status: EscrowStatus         # "locked" | "released" | "slashed"


def _utc_z_now() -> str:
    """Return the current time as `YYYY-MM-DDTHH:MM:SSZ` (no sub-seconds)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class MockSettlementAdapter:
    """In-memory settlement adapter. Structurally implements `SettlementAdapter`.

    Construct with the tuple of assets it handles. Fund principals
    explicitly via `fund` before any lock; `lock` deducts from the named
    principal's balance. `release` credits the destination principal;
    `slash` sends a fraction to burn or a beneficiary, and the remainder
    back to the original locker.

    Single-threaded by design: no locks, no reentrancy defense. The
    scenario simulator dispatches sequentially.
    """

    def __init__(
        self,
        supported_assets: tuple[AssetRef, ...],
        *,
        ledger: Any = None,
    ) -> None:
        if not supported_assets:
            raise ValueError(
                "MockSettlementAdapter requires at least one supported AssetRef"
            )
        self._supported_ids: set[str] = {a.asset_id for a in supported_assets}
        self._supported_refs: dict[str, AssetRef] = {
            a.asset_id: a for a in supported_assets
        }
        # Wired in Ticket 9 by SettlementEventLedger; intentionally unused here.
        self._ledger = ledger

        # In-memory state.
        self.balances: dict[tuple[str, str], Money] = {}
        self.escrows: dict[EscrowHandleId, _EscrowRecord] = {}
        self._consumed_nonces: set[str] = set()

    # ------------------------------------------------------------------
    # Capability
    # ------------------------------------------------------------------
    def supports(self, asset: AssetRef) -> bool:
        return asset.asset_id in self._supported_ids

    # ------------------------------------------------------------------
    # Balance
    # ------------------------------------------------------------------
    def balance(self, principal: str, asset: AssetRef) -> Money:
        """Return the current balance for `(principal, asset)`. Zero if unseen."""
        key = (principal, asset.asset_id)
        if key in self.balances:
            return self.balances[key]
        # Use the registered ref — but the caller's `asset` is fine too,
        # since equality is asset_id-driven.
        return Money.zero(asset)

    def fund(self, principal: str, amount: Money) -> None:
        """MOCK-only helper: credit `principal` with `amount`.

        Real adapters derive balances from chain state; the mock needs
        a seeded entry point so tests can establish starting positions.
        Not part of the SettlementAdapter Protocol.
        """
        if not self.supports(amount.asset):
            raise UnsupportedAssetError(
                f"MockSettlementAdapter does not support {amount.asset.asset_id!r}"
            )
        key = (principal, amount.asset.asset_id)
        current = self.balances.get(key, Money.zero(amount.asset))
        self.balances[key] = current + amount

    # ------------------------------------------------------------------
    # Lock
    # ------------------------------------------------------------------
    def lock(
        self,
        amount: Money,
        ref: str,
        *,
        nonce: str,
        principal: str,
    ) -> EscrowHandle:
        """Lock `amount` from `principal` under external `ref`.

        Extends the SettlementAdapter protocol with a mock-only
        `principal` kwarg; real adapters infer the locker from wallet
        context.

        Raises:
            EscrowStateError: nonce was already consumed (replay).
            UnsupportedAssetError: adapter does not support the asset.
            ValueError: insufficient balance.
        """
        if nonce in self._consumed_nonces:
            raise EscrowStateError("nonce replay detected")
        if not self.supports(amount.asset):
            raise UnsupportedAssetError(
                f"MockSettlementAdapter does not support {amount.asset.asset_id!r}"
            )

        key = (principal, amount.asset.asset_id)
        current = self.balances.get(key, Money.zero(amount.asset))
        if current.quantity < amount.quantity:
            raise ValueError(
                f"insufficient balance for {principal!r}: have "
                f"{current.to_dict()}, need {amount.to_dict()}"
            )

        # Debit the locker; consume nonce; record escrow.
        self.balances[key] = current - amount
        self._consumed_nonces.add(nonce)

        handle_id = EscrowHandleId(uuid.uuid4().hex)
        handle = EscrowHandle(
            handle_id=handle_id,
            asset=amount.asset,
            locked_amount=amount,
            ref=ref,
        )
        self.escrows[handle_id] = _EscrowRecord(
            handle=handle,
            locker=principal,
            status="locked",
        )

        self._record_event(
            kind="lock",
            handle_id=str(handle_id),
            asset_id=amount.asset.asset_id,
            amount_quantity_str=str(amount.quantity),
            sla_id=ref,
            outcome_receipt=None,
            metadata={"locker": principal, "nonce": nonce},
        )
        return handle

    # ------------------------------------------------------------------
    # Release
    # ------------------------------------------------------------------
    def release(self, handle: EscrowHandle, to: str) -> SettlementReceipt:
        """Release the escrow to principal `to`. Credits their balance."""
        # Fetch locker for metadata before _do_release mutates status.
        record = self.escrows.get(handle.handle_id)
        if record is None:
            raise EscrowStateError(
                f"unknown escrow handle {handle.handle_id!r}"
            )
        locker = record.locker
        amount = record.handle.locked_amount
        asset = record.handle.asset

        self._do_release(handle, to=to)

        receipt = SettlementReceipt(
            handle_id=handle.handle_id,
            outcome="released",
            to=to,
            transferred=amount,
            burned=Money.zero(asset),
            ts=_utc_z_now(),
        )
        self._record_event(
            kind="release",
            handle_id=str(handle.handle_id),
            asset_id=asset.asset_id,
            amount_quantity_str=str(amount.quantity),
            sla_id=handle.ref,
            outcome_receipt=receipt.to_dict(),
            metadata={"locker": locker, "to": to},
        )
        return receipt

    # ------------------------------------------------------------------
    # Slash
    # ------------------------------------------------------------------
    def slash(
        self,
        handle: EscrowHandle,
        percent: int,
        beneficiary: str | None,
    ) -> SettlementReceipt:
        """Slash `percent`% of the escrow. Remainder returns to original locker.

        If `beneficiary is None`, the slashed fraction is burned.
        Otherwise it is transferred to `beneficiary`.
        """
        record = self.escrows.get(handle.handle_id)
        if record is None:
            raise EscrowStateError(
                f"unknown escrow handle {handle.handle_id!r}"
            )
        if record.status != "locked":
            raise EscrowStateError(
                f"cannot slash escrow {handle.handle_id!r} in state "
                f"{record.status!r}"
            )
        if not (0 <= percent <= 100):
            raise ValueError(f"slash percent must be in [0, 100], got {percent}")

        locker = record.locker
        amount = record.handle.locked_amount
        asset = record.handle.asset

        # Compute split. Money * Decimal quantizes to asset precision.
        from decimal import Decimal
        slashed_fraction = Decimal(percent) / Decimal(100)
        remainder_fraction = Decimal(100 - percent) / Decimal(100)
        slashed_amount = amount * slashed_fraction
        remainder_amount = amount * remainder_fraction

        # Credit remainder back to the original locker.
        locker_key = (record.locker, asset.asset_id)
        locker_bal = self.balances.get(locker_key, Money.zero(asset))
        self.balances[locker_key] = locker_bal + remainder_amount

        if beneficiary is None:
            # Burn path: slashed fraction is destroyed (no credit).
            transferred = Money.zero(asset)
            burned = slashed_amount
            to = ""
        else:
            # Transfer path: credit beneficiary's balance.
            ben_key = (beneficiary, asset.asset_id)
            ben_bal = self.balances.get(ben_key, Money.zero(asset))
            self.balances[ben_key] = ben_bal + slashed_amount
            transferred = slashed_amount
            burned = Money.zero(asset)
            to = beneficiary

        record.status = "slashed"

        receipt = SettlementReceipt(
            handle_id=handle.handle_id,
            outcome="slashed",
            to=to,
            transferred=transferred,
            burned=burned,
            ts=_utc_z_now(),
        )
        self._record_event(
            kind="slash",
            handle_id=str(handle.handle_id),
            asset_id=asset.asset_id,
            amount_quantity_str=str(amount.quantity),
            sla_id=handle.ref,
            outcome_receipt=receipt.to_dict(),
            metadata={
                "locker": locker,
                "beneficiary": beneficiary or "",
                "percent": percent,
            },
        )
        return receipt

    # ------------------------------------------------------------------
    # Private transfer helpers (shared by release, slash, and
    # release_pending_verdict so verdict-kinded events can be emitted
    # separately from the balance movement).
    # ------------------------------------------------------------------
    def _do_release(self, handle: EscrowHandle, to: str) -> None:
        """Credit `to` with the locked amount; mark escrow released.

        Does NOT emit any ledger event. Callers are responsible for emitting
        the appropriate event after calling this helper.

        Raises:
            EscrowStateError: handle unknown or not in `locked` state.
        """
        record = self.escrows.get(handle.handle_id)
        if record is None:
            raise EscrowStateError(
                f"unknown escrow handle {handle.handle_id!r}"
            )
        if record.status != "locked":
            raise EscrowStateError(
                f"cannot release escrow {handle.handle_id!r} in state "
                f"{record.status!r}"
            )
        amount = record.handle.locked_amount
        asset = record.handle.asset
        dest_key = (to, asset.asset_id)
        current = self.balances.get(dest_key, Money.zero(asset))
        self.balances[dest_key] = current + amount
        record.status = "released"

    def _do_transfer_to(
        self,
        handle: EscrowHandle,
        to: str,
        *,
        percent: int = 100,
    ) -> None:
        """Transfer `percent`% of locked funds to `to`; remainder to locker.

        Does NOT emit any ledger event. Callers are responsible for emitting
        the appropriate event. Marks the escrow as `slashed`.

        Raises:
            EscrowStateError: handle unknown or not in `locked` state.
            ValueError: percent not in [0, 100].
        """
        record = self.escrows.get(handle.handle_id)
        if record is None:
            raise EscrowStateError(
                f"unknown escrow handle {handle.handle_id!r}"
            )
        if record.status != "locked":
            raise EscrowStateError(
                f"cannot slash escrow {handle.handle_id!r} in state "
                f"{record.status!r}"
            )
        if not (0 <= percent <= 100):
            raise ValueError(f"slash percent must be in [0, 100], got {percent}")

        from decimal import Decimal
        amount = record.handle.locked_amount
        asset = record.handle.asset
        slashed_fraction = Decimal(percent) / Decimal(100)
        remainder_fraction = Decimal(100 - percent) / Decimal(100)
        slashed_amount = amount * slashed_fraction
        remainder_amount = amount * remainder_fraction

        # Remainder back to locker.
        locker_key = (record.locker, asset.asset_id)
        locker_bal = self.balances.get(locker_key, Money.zero(asset))
        self.balances[locker_key] = locker_bal + remainder_amount

        # Slashed fraction to `to`.
        dest_key = (to, asset.asset_id)
        dest_bal = self.balances.get(dest_key, Money.zero(asset))
        self.balances[dest_key] = dest_bal + slashed_amount

        record.status = "slashed"

    def _do_release_to_locker(self, handle: EscrowHandle) -> None:
        """Return the full locked amount back to the original locker.

        Used for the `refunded` result path: no slash, no penalty.
        Does NOT emit any ledger event.

        Raises:
            EscrowStateError: handle unknown or not in `locked` state.
        """
        record = self.escrows.get(handle.handle_id)
        if record is None:
            raise EscrowStateError(
                f"unknown escrow handle {handle.handle_id!r}"
            )
        if record.status != "locked":
            raise EscrowStateError(
                f"cannot refund escrow {handle.handle_id!r} in state "
                f"{record.status!r}"
            )
        amount = record.handle.locked_amount
        asset = record.handle.asset
        locker_key = (record.locker, asset.asset_id)
        locker_bal = self.balances.get(locker_key, Money.zero(asset))
        self.balances[locker_key] = locker_bal + amount
        record.status = "released"

    # ------------------------------------------------------------------
    # release_pending_verdict (Ticket A5)
    # ------------------------------------------------------------------
    def release_pending_verdict(
        self,
        handle: EscrowHandle,
        verdict: Any,
        *,
        expected_artifact_hash: str,
        requester_did: str,
        provider_did: str,
    ) -> SettlementReceipt:
        """Settle an escrow based on a signed OracleVerdict.

        Enforces sla_id binding, signature validity, artifact hash binding,
        and double-verdict prevention. Emits verdict_issued (and optionally
        founder_override) before the settlement event.

        Parameters
        ----------
        handle:
            Escrow handle returned from `lock`.
        verdict:
            Signed OracleVerdict from the oracle pipeline.
        expected_artifact_hash:
            The SLA's `artifact_hash_at_delivery`. Must match
            `verdict.artifact_hash`.
        requester_did:
            DID of the requester (used as beneficiary on rejection, and
            recorded in ledger events).
        provider_did:
            DID of the provider (credited on acceptance).

        Returns
        -------
        SettlementReceipt
            Final settlement record.

        Raises
        ------
        VerdictError:
            sla_id mismatch, artifact_hash mismatch, or double-verdict
            without a valid Tier 3 founder override.
        SignatureError:
            Cryptographic verification failed.
        EscrowStateError:
            Escrow not in `locked` state.
        """
        # --- guard: sla_id binding -----------------------------------------
        if verdict.sla_id != handle.ref:
            raise VerdictError(
                f"verdict sla_id {verdict.sla_id!r} does not match "
                f"escrow ref {handle.ref!r}"
            )

        # --- guard: signature -----------------------------------------------
        verdict.verify_signature()  # raises SignatureError on failure

        # --- guard: artifact hash binding ------------------------------------
        if verdict.artifact_hash != expected_artifact_hash:
            raise VerdictError(
                f"artifact hash mismatch: verdict={verdict.artifact_hash!r}, "
                f"expected={expected_artifact_hash!r}"
            )

        # --- guard: double-verdict ------------------------------------------
        sla_id = handle.ref
        if self._ledger is not None:
            prior_verdict_events = [
                ev
                for ev in self._ledger.events()
                if ev.kind == "verdict_issued" and ev.sla_id == sla_id
            ]
            if prior_verdict_events:
                # Allow only a Tier 3 founder override that references the
                # prior verdict's hash via evidence.overrides.
                is_override = (
                    verdict.tier == 3
                    and verdict.evidence.get("kind") == "founder_override"
                    and verdict.evidence.get("overrides") in {
                        ev.metadata.get("verdict_hash")
                        for ev in prior_verdict_events
                    }
                )
                if not is_override:
                    raise VerdictError(
                        f"verdict already issued for sla_id {sla_id!r}"
                    )

        # --- record is fetched for receipt construction below ---------------
        record = self.escrows.get(handle.handle_id)
        if record is None:
            raise EscrowStateError(
                f"unknown escrow handle {handle.handle_id!r}"
            )

        amount = record.handle.locked_amount
        asset = record.handle.asset

        # --- emit verdict_issued event first --------------------------------
        verdict_meta: dict = {
            "verdict_hash": verdict.verdict_hash,
            "tier": verdict.tier,
            "result": verdict.result,
            "evaluator_did": verdict.evaluator_did,
            "evidence_kind": verdict.evidence.get("kind", ""),
        }
        if verdict.tier == 3:
            verdict_meta["overrides"] = verdict.evidence.get("overrides", "")

        self._record_event(
            kind="verdict_issued",
            handle_id=str(handle.handle_id),
            asset_id=asset.asset_id,
            amount_quantity_str=str(amount.quantity),
            sla_id=sla_id,
            outcome_receipt=None,
            metadata=dict(verdict_meta, requester_did=requester_did, provider_did=provider_did),
        )

        # --- emit founder_override event for Tier 3 overrides ---------------
        if (
            verdict.tier == 3
            and verdict.evidence.get("kind") == "founder_override"
        ):
            self._record_event(
                kind="founder_override",
                handle_id=str(handle.handle_id),
                asset_id=asset.asset_id,
                amount_quantity_str=str(amount.quantity),
                sla_id=sla_id,
                outcome_receipt=None,
                metadata={
                    "founder_identity": verdict.evidence.get("founder_identity", ""),
                    "reason": verdict.evidence.get("reason", ""),
                    "overrides": verdict.evidence.get("overrides", ""),
                },
            )

        # --- perform settlement based on result -----------------------------
        result = verdict.result

        if result == "accepted":
            self._do_release(handle, to=provider_did)
            receipt = SettlementReceipt(
                handle_id=handle.handle_id,
                outcome="released",
                to=provider_did,
                transferred=amount,
                burned=Money.zero(asset),
                ts=_utc_z_now(),
            )
            self._record_event(
                kind="release_from_verdict",
                handle_id=str(handle.handle_id),
                asset_id=asset.asset_id,
                amount_quantity_str=str(amount.quantity),
                sla_id=sla_id,
                outcome_receipt=receipt.to_dict(),
                metadata={
                    "requester_did": requester_did,
                    "provider_did": provider_did,
                    "verdict_hash": verdict.verdict_hash,
                },
            )

        elif result == "rejected":
            self._do_transfer_to(handle, to=requester_did, percent=100)
            receipt = SettlementReceipt(
                handle_id=handle.handle_id,
                outcome="slashed",
                to=requester_did,
                transferred=amount,
                burned=Money.zero(asset),
                ts=_utc_z_now(),
            )
            self._record_event(
                kind="slash_from_verdict",
                handle_id=str(handle.handle_id),
                asset_id=asset.asset_id,
                amount_quantity_str=str(amount.quantity),
                sla_id=sla_id,
                outcome_receipt=receipt.to_dict(),
                metadata={
                    "requester_did": requester_did,
                    "provider_did": provider_did,
                    "verdict_hash": verdict.verdict_hash,
                },
            )

        else:  # refunded
            self._do_release_to_locker(handle)
            receipt = SettlementReceipt(
                handle_id=handle.handle_id,
                outcome="released",
                to=record.locker,
                transferred=amount,
                burned=Money.zero(asset),
                ts=_utc_z_now(),
            )
            self._record_event(
                kind="refund_from_verdict",
                handle_id=str(handle.handle_id),
                asset_id=asset.asset_id,
                amount_quantity_str=str(amount.quantity),
                sla_id=sla_id,
                outcome_receipt=receipt.to_dict(),
                metadata={
                    "requester_did": requester_did,
                    "provider_did": provider_did,
                    "verdict_hash": verdict.verdict_hash,
                },
            )

        return receipt

    # ------------------------------------------------------------------
    # Ledger wiring (Ticket 9)
    # ------------------------------------------------------------------
    def _record_event(
        self,
        *,
        kind: str,
        handle_id: str,
        asset_id: str,
        amount_quantity_str: str,
        sla_id: str,
        outcome_receipt: dict | None,
        metadata: dict,
    ) -> None:
        """Emit a `SettlementEvent` to the attached ledger, if any.

        The ledger argument is optional (see constructor); when absent
        this is a no-op and the adapter behaves identically to the
        pre-Ticket-9 implementation. The import is lazy to keep the
        adapter import cycle-safe with `core.primitives.settlement_ledger`.
        """
        if self._ledger is None:
            return
        # Lazy import — ledger module may not be loaded yet and we want
        # to avoid a reverse dep at module-import time.
        from core.primitives.settlement_ledger import SettlementEvent

        event = SettlementEvent(
            kind=kind,  # type: ignore[arg-type]
            handle_id=handle_id,
            asset_id=asset_id,
            amount_quantity_str=amount_quantity_str,
            sla_id=sla_id,
            principals={
                "requester_did": "",
                "provider_did": "",
                "counterparty_pubkey_hex": "",
            },
            outcome_receipt=outcome_receipt,
            metadata=dict(metadata),
        )
        self._ledger.record(event)

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------
    def get_status(self, handle: EscrowHandle) -> EscrowStatus:
        """Lifecycle state of `handle`. Unknown raises EscrowStateError."""
        record = self.escrows.get(handle.handle_id)
        if record is None:
            raise EscrowStateError(
                f"unknown escrow handle {handle.handle_id!r}"
            )
        return record.status


__all__ = ["MockSettlementAdapter"]
