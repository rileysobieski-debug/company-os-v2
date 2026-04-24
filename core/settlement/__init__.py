"""Settlement composition layer (Weeks 6-7).

The primitive layer (`core.primitives`) already ships Money, AssetRef,
SettlementAdapter, EscrowHandle, SettlementReceipt, and a working
MockSettlementAdapter. This package is the NEXT layer up: composition
code that assembles those primitives into the v6 "atomic SLA-Escrow-
Citation loop" and wires them behind an x402-shaped HTTP 402 payment
interface.

x402 (`core.settlement.x402_mock.X402MockAdapter`) models the protocol
an agent talks to when it needs to pay for a resource. The mock runs
entirely in-process so tests and local dev exercise the full flow
without real Coinbase Agentic Wallet credentials; the v6 plan pins
the live Coinbase integration to Weeks 6-7 ship with a testnet wallet,
which ships in a follow-up PR once credentials are provisioned.

Atomic loop (`core.settlement.atomic_loop.settle_with_atomic_citation`)
composes:

    1. PVE decision must be APPROVE (otherwise auto-deny, no lock).
    2. Lock escrow on the settlement adapter.
    3. Run the caller-supplied handler.
    4. On handler success: write the citation row AND release the
       escrow as a single unit. Either both succeed or both roll back.
    5. On handler failure or citation failure: refund the escrow.

This closes rubric criterion #6: Atomic Financial Settlement. The
invariant under test is `no handler outcome leaves the escrow in an
ambiguous state`: every escrow either releases (payee paid, citation
written) or refunds (payee not paid, no citation), never both and
never neither.
"""
from __future__ import annotations

from core.settlement.atomic_loop import (
    AtomicLoopError,
    AtomicLoopResult,
    CitationWriteFailure,
    HandlerFailure,
    LoopOutcome,
    settle_with_atomic_citation,
)
from core.settlement.x402_mock import (
    PaymentReceipt,
    PaymentRequirement,
    X402Error,
    X402MockAdapter,
    X402PaymentExpired,
    X402PaymentInsufficient,
    X402PaymentInvalid,
)

__all__ = [
    "AtomicLoopError",
    "AtomicLoopResult",
    "CitationWriteFailure",
    "HandlerFailure",
    "LoopOutcome",
    "PaymentReceipt",
    "PaymentRequirement",
    "X402Error",
    "X402MockAdapter",
    "X402PaymentExpired",
    "X402PaymentInsufficient",
    "X402PaymentInvalid",
    "settle_with_atomic_citation",
]
