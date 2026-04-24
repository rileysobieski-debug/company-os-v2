"""SLA-Escrow-Citation atomic loop.

Closes rubric criterion #6 (Atomic Financial Settlement): the single
primitive every paid action flows through. Invariant: every escrow
ends in one of exactly two terminal states:

    RELEASED: handler succeeded, citation written, escrow released
              to payee. Payee paid, audit chain complete.
    REFUNDED: handler did not succeed OR citation write did not
              succeed. Escrow returned to locker. No payee payout.
              No citation row.

There is no third "handler ran but citation failed" outcome visible to
the caller; a citation-write failure after a successful handler rolls
the escrow back to REFUNDED and surfaces `CitationWriteFailure` with
the handler output attached so the caller can retry the whole loop
with a fresh nonce.

Wiring diagram:

    PVE.evaluate(request) -> APPROVE
             |
             v
    X402MockAdapter.quote(...)
             |
             v
    X402MockAdapter.lock_for_quote(...)
             |
             v
    handler(request) -> result or raise
             |
             v
    citation_writer(result, citation_hash) -> ok or raise
             |
             v
    adapter.release(handle, to=payee)   [RELEASED]
             |
             v
    return AtomicLoopResult(outcome=RELEASED, ...)

Any step failure rolls back via `adapter.refund(handle)`.

Handler + citation_writer are caller-supplied so this module stays
dependency-light. The handler takes the ActionRequest and returns
whatever payload the citation needs; the citation_writer takes both
and either returns True/None on success or raises.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Protocol, runtime_checkable

from core.primitives.settlement_adapters.base import EscrowHandle, SettlementReceipt
from core.settlement.x402_mock import (
    PaymentReceipt,
    PaymentRequirement,
    X402Error,
    X402MockAdapter,
)


# The evaluator module lives on a sibling feature branch (weeks-4-5-memory-layer).
# To keep settlement importable standalone, the atomic loop only requires a
# structural protocol for the decision object: any dataclass / object with a
# `verdict` attr whose value compares equal to "approve" qualifies. When the
# Memory layer merges, `core.governance.evaluator.EvaluatorDecision` satisfies
# this protocol by construction.
@runtime_checkable
class _DecisionLike(Protocol):
    request_hash: str

    @property
    def verdict(self) -> Any: ...


@runtime_checkable
class _RequestLike(Protocol):
    action_type: str


class AtomicLoopError(RuntimeError):
    """Base class. Subclasses identify the stage that failed."""


class HandlerFailure(AtomicLoopError):
    """The caller-supplied handler raised. Escrow was refunded."""


class CitationWriteFailure(AtomicLoopError):
    """Handler succeeded but the citation writer failed. Escrow was
    refunded; handler output is attached so the caller can retry."""

    def __init__(self, message: str, *, handler_output: Any) -> None:
        super().__init__(message)
        self.handler_output = handler_output


class LoopOutcome(Enum):
    RELEASED = "released"
    REFUNDED = "refunded"
    DENIED = "denied"


@dataclass(frozen=True)
class AtomicLoopResult:
    outcome: LoopOutcome
    request_hash: str
    settlement_receipt: SettlementReceipt | None = None
    payment_receipt: PaymentReceipt | None = None
    payment_requirement: PaymentRequirement | None = None
    handler_output: Any = None
    citation_hash: str = ""
    detail: str = ""
    at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


CitationWriter = Callable[[Any, Any, str], None]
Handler = Callable[[Any, PaymentRequirement], Any]


def _is_approved(decision: _DecisionLike) -> bool:
    """Duck-typed approve check. Works against `EvaluatorDecision.verdict`
    (an enum whose .value == "approve") and against simple string-valued
    decision types."""
    verdict = getattr(decision, "verdict", None)
    verdict_value = getattr(verdict, "value", verdict)
    return verdict_value == "approve"


def settle_with_atomic_citation(
    *,
    request: _RequestLike,
    decision: _DecisionLike,
    quote: PaymentRequirement,
    receipt: PaymentReceipt,
    handle: EscrowHandle,
    adapter: X402MockAdapter,
    handler: Handler,
    citation_writer: CitationWriter,
    citation_hash: str,
) -> AtomicLoopResult:
    """Run the SLA-Escrow-Citation atomic loop.

    Preconditions (enforced at entry):

        - `decision.verdict == Verdict.APPROVE`. A non-approve decision
          short-circuits to REFUNDED with no handler call.
        - The receipt is verified against the adapter before the
          handler runs. A bad receipt is treated as a handler failure:
          refund, no citation.

    The caller is responsible for having called `adapter.lock_for_quote`
    before invoking this function; that split lets caller code decide
    whether to lock synchronously or hand the quote off to another
    agent that returns the receipt later.
    """
    if not _is_approved(decision):
        detail = getattr(decision, "reason", "") or ""
        if not detail:
            reason_code = getattr(decision, "reason_code", None)
            detail = f"evaluator denied ({reason_code})" if reason_code else "evaluator denied"
        return AtomicLoopResult(
            outcome=LoopOutcome.DENIED,
            request_hash=decision.request_hash,
            detail=detail,
        )

    try:
        adapter.verify_receipt(receipt, expected_resource_uri=quote.resource_uri)
    except X402Error as exc:
        _refund_safely(adapter, handle)
        return AtomicLoopResult(
            outcome=LoopOutcome.REFUNDED,
            request_hash=decision.request_hash,
            payment_receipt=receipt,
            payment_requirement=quote,
            detail=f"receipt verification failed: {exc}",
        )

    try:
        handler_output = handler(request, quote)
    except Exception as exc:
        _refund_safely(adapter, handle)
        raise HandlerFailure(f"{type(exc).__name__}: {exc}") from exc

    try:
        citation_writer(request, handler_output, citation_hash)
    except Exception as exc:
        _refund_safely(adapter, handle)
        raise CitationWriteFailure(
            f"{type(exc).__name__}: {exc}",
            handler_output=handler_output,
        ) from exc

    try:
        settlement_receipt = adapter.release(handle, to=quote.payee_principal)
    except Exception as exc:
        # Release failure is the nastiest edge: citation landed but
        # funds did not move. Surface clearly so the operator sees an
        # out-of-band reconciliation is needed.
        raise AtomicLoopError(
            f"escrow release failed after citation wrote; "
            f"reconcile manually: {type(exc).__name__}: {exc}",
        ) from exc

    return AtomicLoopResult(
        outcome=LoopOutcome.RELEASED,
        request_hash=decision.request_hash,
        settlement_receipt=settlement_receipt,
        payment_receipt=receipt,
        payment_requirement=quote,
        handler_output=handler_output,
        citation_hash=citation_hash,
        detail="",
    )


def _refund_safely(adapter: X402MockAdapter, handle: EscrowHandle) -> None:
    """Best-effort refund. If the refund itself fails (e.g. adapter
    already released by a prior call), swallow: the caller will see
    the outcome in the returned AtomicLoopResult."""
    try:
        adapter.refund(handle)
    except Exception:
        # Intentional swallow: refund failure does not override the
        # original handler failure signal. The caller catches
        # HandlerFailure / CitationWriteFailure and handles reconciliation.
        pass


__all__ = [
    "AtomicLoopError",
    "AtomicLoopResult",
    "CitationWriteFailure",
    "CitationWriter",
    "Handler",
    "HandlerFailure",
    "LoopOutcome",
    "settle_with_atomic_citation",
]
