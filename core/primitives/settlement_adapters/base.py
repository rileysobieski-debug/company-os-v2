"""
core/primitives/settlement_adapters/base.py — support types for settlement
===========================================================================

Ticket 0: the small, currency-agnostic shapes that every adapter needs.

- `EscrowHandleId` (NewType over `str`): opaque handle id. V0 populates
  with `uuid4().hex`; V1 may populate with an on-chain tx hash. Callers
  must never inspect the internal structure — treat it like an opaque
  pointer.
- `EscrowStatus`: the three terminal states an escrow can reach.
- `EscrowHandle`: the opaque receipt returned from `lock()`. Carries
  just enough to finalize later without re-deriving state.
- `SettlementReceipt`: the final record of a `release()` or `slash()`
  action. Used as evidence in the scenario ledger / audit trail.

`AssetRef` (Ticket 1) and `Money` (Ticket 2) do not exist yet; they are
forward-referenced via string annotations. `from __future__ import
annotations` keeps the annotations stringified at runtime so importing
this module does not require those types.

Canonical serialization rules (shared with `core.primitives.integrity`):
- `json.dumps(..., sort_keys=True, separators=(",", ":"), ensure_ascii=False)`
- `Decimal` values serialize via `str(d)`, never float.
- Datetimes normalize to UTC-Z form `YYYY-MM-DDTHH:MM:SSZ`.
- `Money` serializes to `{"quantity": "<str>", "asset_id": "<str>"}`.

`to_dict` returns a plain dict ready for `json.dumps`; callers that want
the canonical string should call `json.dumps(handle.to_dict(), ...)`
with the rules above.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal, NewType, Protocol, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover — type-check only
    from core.primitives.asset import AssetRef       # Ticket 1
    from core.primitives.money import Money          # Ticket 2
    from core.primitives.oracle import OracleVerdict  # A2


# ---------------------------------------------------------------------------
# Identifiers and status
# ---------------------------------------------------------------------------
EscrowHandleId = NewType("EscrowHandleId", str)
"""Opaque escrow handle id.

V0: `uuid4().hex`.
V1: on-chain tx hash (hex).
Callers must NOT inspect internal structure; treat as an opaque token.
"""

EscrowStatus = Literal["locked", "released", "slashed"]
"""Terminal-ish state of an escrow lifecycle.

- `locked`   — funds are held; neither party can unilaterally withdraw.
- `released` — funds were paid out to the intended recipient.
- `slashed`  — funds were burned (or partially transferred + burned) as
               an SLA penalty.
"""


# ---------------------------------------------------------------------------
# Canonical JSON helpers
# ---------------------------------------------------------------------------
def _dumps_canonical(obj: Any) -> str:
    """Canonical JSON dump per project settlement serialization rules."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _money_to_dict(m: Any) -> dict[str, str]:
    """Serialize a Money-shaped object to its canonical dict form.

    Accepts any duck-typed object exposing `.quantity` (str-coercible,
    typically `Decimal`) and `.asset` with `.asset_id`. This keeps Ticket 0
    independent of Ticket 2's concrete `Money` class.
    """
    # `.quantity` may be Decimal (Ticket 2) — coerce via str() to avoid float.
    quantity = str(m.quantity)
    asset_id = str(m.asset.asset_id)
    return {"quantity": quantity, "asset_id": asset_id}


# ---------------------------------------------------------------------------
# Escrow handle
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class EscrowHandle:
    """Opaque receipt returned from an adapter's `lock()` call.

    Equality and hashability flow from dataclass(frozen=True); `Money`
    and `AssetRef` (when they land) must also be hashable for this to
    compose cleanly, which Tickets 1 and 2 guarantee.
    """

    handle_id: EscrowHandleId
    asset: "AssetRef"
    locked_amount: "Money"
    ref: str  # external reference, e.g. sla_id

    def to_dict(self) -> dict[str, Any]:
        """Canonical dict form. `json.dumps(..., sort_keys=True,
        separators=(",", ":"), ensure_ascii=False)` over this result
        yields the canonical serialization string."""
        return {
            "handle_id": str(self.handle_id),
            "asset_id": str(self.asset.asset_id),
            "locked_amount": _money_to_dict(self.locked_amount),
            "ref": self.ref,
        }

    def to_canonical_json(self) -> str:
        return _dumps_canonical(self.to_dict())


# ---------------------------------------------------------------------------
# Settlement receipt
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SettlementReceipt:
    """Final record of a settlement action (release or slash).

    `outcome == "released"` implies `burned` is zero-Money and
    `transferred` carries the payout. `outcome == "slashed"` allows any
    split between `transferred` and `burned`; fully-burned slashes set
    `to == ""` and `transferred` to zero-Money.

    `ts` is ISO-8601 UTC with explicit offset. Callers should normalize
    to `YYYY-MM-DDTHH:MM:SSZ` before constructing — we don't reformat
    here because that would hide bugs in the caller's clock handling.
    """

    handle_id: EscrowHandleId
    outcome: Literal["released", "slashed"]
    to: str              # principal credited; "" when fully burned
    transferred: "Money"
    burned: "Money"
    ts: str              # ISO-8601 UTC, e.g. "2026-04-19T12:34:56Z"

    def to_dict(self) -> dict[str, Any]:
        return {
            "handle_id": str(self.handle_id),
            "outcome": self.outcome,
            "to": self.to,
            "transferred": _money_to_dict(self.transferred),
            "burned": _money_to_dict(self.burned),
            "ts": self.ts,
        }

    def to_canonical_json(self) -> str:
        return _dumps_canonical(self.to_dict())


# ---------------------------------------------------------------------------
# SettlementAdapter protocol (Ticket 3)
# ---------------------------------------------------------------------------
@runtime_checkable
class SettlementAdapter(Protocol):
    """Structural contract for any settlement backend.

    Adapters implement escrow mechanics for one or more assets. Capability
    is declared dynamically via `supports(asset)` rather than a static
    `asset_id` field so a single adapter can cover a family of assets
    (e.g. a future EVM adapter handling USDC, DAI, and ETH on the same
    chain).

    Callers must dispatch exclusively through an `AdapterRegistry`:

        registry.adapter_for(money.asset).lock(money, ref, nonce=...)

    Never bind a direct adapter reference per asset — the registry owns
    capability resolution and conflict detection.

    Notes on method contracts:
    - `lock` takes a keyword-only `nonce: str` parameter. Adapters MUST
      treat nonce reuse as an error (`EscrowStateError("nonce replay
      detected")`), even if other fields of the call differ. This
      protects the settlement boundary against replayed SLA fulfillment
      messages.
    - `slash` accepts `percent` in [0, 100] and an optional beneficiary.
      When `beneficiary is None`, the slashed fraction is burned; when
      provided, it is transferred to the beneficiary. The remainder in
      both cases returns to the original locker.
    - `balance(principal, asset)` is explicit — no ambient asset. An
      unseen principal returns zero-Money at the asset's precision.
    - `get_status(handle)` is for observability and future dispute logic.
      Unknown handles raise `EscrowStateError`.
    """

    def supports(self, asset: "AssetRef") -> bool:
        """Return True iff this adapter can settle `asset`."""
        ...

    def lock(
        self, amount: "Money", ref: str, *, nonce: str
    ) -> EscrowHandle:
        """Lock `amount` against external reference `ref` with replay-resistant `nonce`."""
        ...

    def release(self, handle: EscrowHandle, to: str) -> SettlementReceipt:
        """Release a locked escrow to principal `to`."""
        ...

    def slash(
        self, handle: EscrowHandle, percent: int, beneficiary: str | None
    ) -> SettlementReceipt:
        """Slash `percent`% of a locked escrow. Remainder returns to locker."""
        ...

    def balance(self, principal: str, asset: "AssetRef") -> "Money":
        """Current balance held for `principal` in `asset`. Zero if unseen."""
        ...

    def get_status(self, handle: EscrowHandle) -> EscrowStatus:
        """Observed lifecycle state of `handle`. Raises EscrowStateError on unknown."""
        ...

    def release_pending_verdict(
        self,
        handle: EscrowHandle,
        verdict: "OracleVerdict",
        *,
        expected_artifact_hash: str,
        requester_did: str,
        provider_did: str,
        now: "datetime | None" = None,
        challenge_window_sec: "int | None" = None,
        expected_primary_evaluator_did: "str | None" = None,
        expected_evaluator_canonical_hash: "str | None" = None,
    ) -> SettlementReceipt:
        """Settle an escrow based on a signed `OracleVerdict`.

        Enforces:
        - `verdict.sla_id == handle.ref` (mismatched SLA raises VerdictError).
        - `verdict.verify_signature()` passes (tampered verdict raises
          SignatureError).
        - `verdict.artifact_hash == expected_artifact_hash` (hash binding raises
          VerdictError on mismatch).

        For Tier 1 verdicts with `challenge_window_sec` set:
        - Validates evaluator DID and canonical hash if expected values provided.
        - Enforces the challenge window: raises ChallengeWindowError if window
          is still open or an unresolved challenge exists.

        Result dispatch:
        - `accepted`  -> release escrow to `provider_did`.
        - `rejected`  -> 100% slash to `requester_did` as beneficiary.
        - `refunded`  -> return escrow to the original locker, no slash.

        Emits a `verdict_issued` ledger event before the settlement event.
        For Tier 3 `founder_override` verdicts also emits `founder_override`.
        For Tier 3 verdicts superseding a challenge, emits `challenge_resolved`
        before the settlement event sequence.

        Raises:
            VerdictError: sla_id mismatch, artifact_hash mismatch, or
                double-verdict without a valid Tier 3 override.
            SignatureError: cryptographic verification failure.
            EscrowStateError: escrow not in `locked` state.
            EvaluatorAuthorizationError: evaluator DID or canonical hash mismatch.
            ChallengeWindowError: challenge window still open or unresolved
                challenge blocks release.
        """
        ...

    def raise_challenge(
        self,
        handle: EscrowHandle,
        challenge: "Any",
        *,
        requester_did: str,
        provider_did: str,
        prior_verdict: "OracleVerdict",
        challenge_window_sec: int,
    ) -> None:
        """Record a challenge against a Tier 1 verdict within the challenge window.

        Validates the challenge signature, ensures it references the prior verdict,
        verifies the challenger is a party to the SLA, and enforces Ruling 16
        (challenge must be issued within the window). Emits a `challenge_raised`
        ledger event on success.

        Raises:
            SignatureError: challenge signature invalid.
            VerdictError: challenge references wrong verdict or challenger is not
                a counterparty.
            ChallengeWindowError: challenge issued after the window elapsed.
        """
        ...


# ---------------------------------------------------------------------------
# AdapterRegistry (Ticket 3)
# ---------------------------------------------------------------------------
class AdapterRegistry:
    """Owns adapter <-> asset capability resolution.

    Pairs with an `AssetRegistry`. `register(adapter)` walks the paired
    asset registry's known ids and raises `AdapterConflictError` if the
    incoming adapter's `supports()` overlaps with any previously
    registered adapter. `adapter_for(asset)` returns the adapter that
    claims `asset`, or raises `UnsupportedAssetError` when none do.

    Overlap is treated as a configuration error — there is no
    first-match-wins fallback. Callers that want to switch backends for
    an asset must construct a new registry or explicitly swap adapters.
    """

    def __init__(self, asset_registry: Any) -> None:
        # `Any` at the annotation level because AssetRegistry lives in
        # core.primitives.asset; we avoid the hard import to keep this
        # module import-light and cycle-safe.
        self.asset_registry = asset_registry
        self._adapters: list[SettlementAdapter] = []

    def register(self, adapter: SettlementAdapter) -> None:
        """Register `adapter`. Raises AdapterConflictError on overlap.

        Overlap is detected by walking every asset currently known to
        the paired AssetRegistry and asking both the incoming and each
        previously-registered adapter whether they claim it.
        """
        # Lazy import to avoid module-load cycles; AssetRegistry.ids()
        # returns the known asset ids, which we resolve back to AssetRef
        # via the registry's `get`.
        known_ids = list(self.asset_registry.ids())
        for asset_id in known_ids:
            asset_ref = self.asset_registry.get(asset_id)
            if not adapter.supports(asset_ref):
                continue
            for existing in self._adapters:
                if existing.supports(asset_ref):
                    # Avoid importing at module top to keep this file
                    # dependency-light for Ticket 0 importers.
                    from core.primitives.exceptions import AdapterConflictError
                    raise AdapterConflictError(
                        f"adapter conflict: asset {asset_id!r} already "
                        f"claimed by {type(existing).__name__}; refusing "
                        f"to register {type(adapter).__name__}"
                    )
        self._adapters.append(adapter)

    def adapter_for(self, asset: "AssetRef") -> SettlementAdapter:
        """Return the adapter that claims `asset`, else UnsupportedAssetError."""
        for adapter in self._adapters:
            if adapter.supports(asset):
                return adapter
        from core.primitives.exceptions import UnsupportedAssetError
        raise UnsupportedAssetError(
            f"no registered adapter supports asset {asset.asset_id!r}"
        )


__all__ = [
    "AdapterRegistry",
    "EscrowHandle",
    "EscrowHandleId",
    "EscrowStatus",
    "SettlementAdapter",
    "SettlementReceipt",
]
