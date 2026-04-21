"""
core/primitives/exceptions.py — Settlement exception hierarchy
==============================================================

Ticket 0 of the v0 Currency-Agnostic Settlement Architecture.

One flat `SettlementError` root with subclasses that name specific failure
modes. Callers catch the root when they want "any settlement failure"; they
catch a subclass when they want to recover from one specific mode (e.g.
`UnsupportedAssetError` triggers a fallback to a different asset).

Keep this module import-light: stdlib only. Settlement adapters and the
`Money` / `AssetRef` primitives raise these at their boundaries, but none
of them live here — a shallow module protects us from circular imports
when the book is eventually wired through.
"""
from __future__ import annotations


class SettlementError(Exception):
    """Root of the settlement exception hierarchy.

    Any exception raised by a settlement adapter, the adapter registry,
    or the Money / AssetRef primitives when enforcing settlement rules
    should subclass this. External callers can `except SettlementError`
    once to get them all.
    """


class AssetMismatchError(SettlementError):
    """Two `Money` operands disagree on `AssetRef`.

    Raised by `Money.__add__`, `__sub__`, comparison operators, and any
    aggregate that expects a homogeneous asset set. The error message
    should identify both assets so the caller can diagnose the mix.
    """


class UnsupportedAssetError(SettlementError):
    """An adapter was asked to handle an asset it does not support.

    Each adapter advertises the assets it can settle. The registry
    raises this when no registered adapter claims the requested asset,
    or when an adapter is invoked on an asset outside its declared set.
    """


class EscrowStateError(SettlementError):
    """An escrow operation was attempted in an incompatible state.

    Examples: releasing an already-released handle, slashing a
    never-locked handle, double-finalizing a receipt. Carries enough
    context for an operator to tell which transition was rejected.
    """


class InexactQuantizationError(SettlementError):
    """Quantization to an asset's `decimals` would lose precision.

    `Money` uses `Decimal` throughout and refuses to silently round.
    When an arithmetic result has more fractional digits than the
    asset's `decimals` allows, the primitive raises this rather than
    round-tripping through float.
    """


class AdapterConflictError(SettlementError):
    """Two adapters claim the same asset in the registry.

    The registry enforces a unique adapter per asset at registration
    time. Conflicts here indicate a build / configuration error, not a
    runtime recoverable condition.
    """


class SignatureError(SettlementError):
    """A receipt or escrow handle failed signature verification.

    V0 uses hash-backed integrity (see `core.primitives.integrity`). V1
    upgrades to cryptographic signatures over receipts. This error type
    is forward-compatible with both regimes.
    """


class VerdictError(SettlementError):
    """A Tier 0 / Tier 3 oracle verdict failed a structural or binding
    check.

    Raised when `SettlementAdapter.release_pending_verdict` refuses to
    act on a verdict because (a) the verdict's `sla_id` does not match
    the escrow handle's ref, (b) a verdict has already been issued
    against this SLA and the caller is not presenting a Tier 3 founder
    override, (c) the evidence kind is unknown at deserialization time,
    or (d) any other structural violation short of a bad signature
    (which is `SignatureError`'s job).

    Distinct from `SignatureError` so callers can recover from a replay
    or double-issue differently from a cryptographic failure.
    """


class ChallengeError(SettlementError):
    """A Tier 1 evaluator verdict was challenged outside its challenge
    window, or a challenge carried invalid evidence.

    Reserved for v1b. V1a has no Tier 1 evaluator and therefore no
    challenge path, but the `challenge_window_sec` field on
    `InterOrgSLA` is already canonical and forward-compat callers may
    raise this type when they encounter a challenge against a v1a
    adapter. V1a code never raises this; it is carried in the hierarchy
    so downstream modules can import it without a second refactor when
    Tier 1 ships.
    """


class EvaluatorAuthorizationError(SettlementError):
    """An evaluator failed one or more authorization checks before scoring.

    Raised by `Oracle.evaluate_tier1` when:
      - The evaluator's `canonical_hash` does not match the
        `canonical_evaluator_hash` pinned in the SLA.
      - The evaluator's `evaluator_did` equals the SLA's
        `requester_node_did` or `provider_node_did` (counterparty
        conflict: a party to the contract cannot also be its evaluator).

    Distinct from `SignatureError` (which covers bad cryptography) and
    `VerdictError` (which covers structural verdict failures). Callers
    that want to distinguish a hash mismatch from a counterparty conflict
    should inspect the message; the exception type covers both.
    """


__all__ = [
    "SettlementError",
    "AssetMismatchError",
    "UnsupportedAssetError",
    "EscrowStateError",
    "InexactQuantizationError",
    "AdapterConflictError",
    "SignatureError",
    "VerdictError",
    "ChallengeError",
    "EvaluatorAuthorizationError",
]
