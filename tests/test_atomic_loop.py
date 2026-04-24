"""SLA-Escrow-Citation atomic loop tests.

Invariant under test: every paid action ends in either RELEASED or
REFUNDED; never an ambiguous in-between state. Handler / citation /
receipt failures each route to REFUNDED; only all-three-succeed
produces RELEASED.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum

import pytest

# The real EvaluatorDecision lives on the Memory layer branch. Here we build
# structurally-compatible stand-ins so the atomic loop can be exercised
# without that branch being merged. When Memory lands on main, the real
# types drop in unchanged.
class _V(Enum):
    APPROVE = "approve"
    AUTO_DENY = "auto_deny"
    ESCALATE = "escalate"


@dataclass(frozen=True)
class _Decision:
    verdict: _V
    request_hash: str
    reason: str = ""
    reason_code: str = ""


@dataclass(frozen=True)
class _Request:
    action_type: str
    payload: dict = field(default_factory=dict)


def _hash_of(action: str, payload: dict) -> str:
    import hashlib, json
    payload_str = json.dumps({"action": action, "payload": payload}, sort_keys=True)
    return hashlib.sha256(payload_str.encode("utf-8")).hexdigest()


from core.primitives.asset import AssetRef
from core.primitives.money import Money
from core.primitives.settlement_adapters.mock_adapter import MockSettlementAdapter
from core.settlement.atomic_loop import (
    AtomicLoopError,
    CitationWriteFailure,
    HandlerFailure,
    LoopOutcome,
    settle_with_atomic_citation,
)
from core.settlement.x402_mock import X402MockAdapter


USDC = AssetRef(asset_id="USDC", decimals=6)


@pytest.fixture
def settlement() -> MockSettlementAdapter:
    adapter = MockSettlementAdapter(supported_assets=(USDC,))
    adapter.fund("payer", Money(Decimal("100"), USDC))
    return adapter


@pytest.fixture
def x402(settlement) -> X402MockAdapter:
    return X402MockAdapter(settlement)


def _request(action_type: str = "spend_commit") -> _Request:
    return _Request(action_type=action_type, payload={"resource": "/api/dispatch"})


def _approved_decision(req: _Request) -> _Decision:
    return _Decision(
        verdict=_V.APPROVE,
        request_hash=_hash_of(req.action_type, req.payload),
    )


def _denied_decision(req: _Request) -> _Decision:
    return _Decision(
        verdict=_V.AUTO_DENY,
        request_hash=_hash_of(req.action_type, req.payload),
        reason="hard constraint",
        reason_code="hard_constraint",
    )


def _paid(x402, amount=Decimal("5")):
    quote = x402.quote(
        resource_uri="/api/dispatch",
        amount=Money(amount, USDC),
        payee_principal="payee",
    )
    handle, receipt = x402.lock_for_quote(quote.quote_id, payer_principal="payer")
    return quote, handle, receipt


# ---------------------------------------------------------------------------
# RELEASED happy path
# ---------------------------------------------------------------------------
def test_happy_path_releases_to_payee(x402, settlement) -> None:
    req = _request()
    quote, handle, receipt = _paid(x402)
    citations_written: list[str] = []

    def handler(r: _Request, q) -> dict:
        return {"delivered": True, "resource": q.resource_uri}

    def citation_writer(r: _Request, out, ch: str) -> None:
        citations_written.append(ch)

    result = settle_with_atomic_citation(
        request=req,
        decision=_approved_decision(req),
        quote=quote,
        receipt=receipt,
        handle=handle,
        adapter=x402,
        handler=handler,
        citation_writer=citation_writer,
        citation_hash="hash-xyz",
    )
    assert result.outcome == LoopOutcome.RELEASED
    assert result.citation_hash == "hash-xyz"
    assert citations_written == ["hash-xyz"]
    assert settlement.balance("payee", USDC).quantity == Decimal("5")
    assert settlement.balance("payer", USDC).quantity == Decimal("95")


def test_happy_path_returns_handler_output(x402) -> None:
    req = _request()
    quote, handle, receipt = _paid(x402)

    def handler(r, q):
        return {"answer": 42}

    result = settle_with_atomic_citation(
        request=req,
        decision=_approved_decision(req),
        quote=quote,
        receipt=receipt,
        handle=handle,
        adapter=x402,
        handler=handler,
        citation_writer=lambda r, o, ch: None,
        citation_hash="h",
    )
    assert result.outcome == LoopOutcome.RELEASED
    assert result.handler_output == {"answer": 42}


# ---------------------------------------------------------------------------
# Evaluator denied
# ---------------------------------------------------------------------------
def test_denied_decision_short_circuits_without_handler_call(x402) -> None:
    req = _request()
    quote, handle, receipt = _paid(x402)
    handler_called: list[bool] = []
    citation_called: list[bool] = []

    def handler(r, q):
        handler_called.append(True)
        return {}

    def citation_writer(r, o, ch):
        citation_called.append(True)

    result = settle_with_atomic_citation(
        request=req,
        decision=_denied_decision(req),
        quote=quote,
        receipt=receipt,
        handle=handle,
        adapter=x402,
        handler=handler,
        citation_writer=citation_writer,
        citation_hash="h",
    )
    assert result.outcome == LoopOutcome.DENIED
    assert handler_called == []
    assert citation_called == []


# ---------------------------------------------------------------------------
# Receipt verification failure
# ---------------------------------------------------------------------------
def test_invalid_receipt_refunds_and_returns_refunded(x402, settlement) -> None:
    req = _request()
    payer_before = settlement.balance("payer", USDC)
    payee_before = settlement.balance("payee", USDC)
    quote, handle, _receipt = _paid(x402)
    # Forge a receipt that points at a different handle.
    from core.settlement.x402_mock import PaymentReceipt
    forged = PaymentReceipt(
        quote_id=quote.quote_id,
        handle_id="h_forged",
        payer_principal="payer",
        paid_amount=quote.amount,
        signed_at="2026-04-24T00:00:00+00:00",
    )

    result = settle_with_atomic_citation(
        request=req,
        decision=_approved_decision(req),
        quote=quote,
        receipt=forged,
        handle=handle,
        adapter=x402,
        handler=lambda r, q: {"should": "not run"},
        citation_writer=lambda *_: None,
        citation_hash="h",
    )
    assert result.outcome == LoopOutcome.REFUNDED
    assert settlement.balance("payer", USDC) == payer_before
    assert settlement.balance("payee", USDC) == payee_before


# ---------------------------------------------------------------------------
# Handler failure
# ---------------------------------------------------------------------------
def test_handler_exception_refunds_and_raises_handler_failure(x402, settlement) -> None:
    req = _request()
    payer_before = settlement.balance("payer", USDC)
    payee_before = settlement.balance("payee", USDC)
    quote, handle, receipt = _paid(x402)
    citations_written: list[str] = []

    def handler(r, q):
        raise RuntimeError("handler kaboom")

    with pytest.raises(HandlerFailure, match="handler kaboom"):
        settle_with_atomic_citation(
            request=req,
            decision=_approved_decision(req),
            quote=quote,
            receipt=receipt,
            handle=handle,
            adapter=x402,
            handler=handler,
            citation_writer=lambda r, o, ch: citations_written.append(ch),
            citation_hash="h",
        )
    # Citation NOT written on handler failure.
    assert citations_written == []
    # Escrow refunded.
    assert settlement.balance("payer", USDC) == payer_before
    assert settlement.balance("payee", USDC) == payee_before


# ---------------------------------------------------------------------------
# Citation failure after successful handler
# ---------------------------------------------------------------------------
def test_citation_failure_refunds_and_attaches_handler_output(x402, settlement) -> None:
    req = _request()
    payer_before = settlement.balance("payer", USDC)
    payee_before = settlement.balance("payee", USDC)
    quote, handle, receipt = _paid(x402)

    def handler(r, q):
        return {"valuable": "output"}

    def citation_writer(r, o, ch):
        raise OSError("disk full writing citation")

    with pytest.raises(CitationWriteFailure) as excinfo:
        settle_with_atomic_citation(
            request=req,
            decision=_approved_decision(req),
            quote=quote,
            receipt=receipt,
            handle=handle,
            adapter=x402,
            handler=handler,
            citation_writer=citation_writer,
            citation_hash="h",
        )
    # Handler output attached to the exception so the caller can
    # retry the loop with a fresh quote.
    assert excinfo.value.handler_output == {"valuable": "output"}
    # Escrow refunded. Neither payer nor payee balance changed.
    assert settlement.balance("payer", USDC) == payer_before
    assert settlement.balance("payee", USDC) == payee_before


# ---------------------------------------------------------------------------
# Release failure (worst case: citation landed, release did not)
# ---------------------------------------------------------------------------
def test_release_failure_raises_atomic_loop_error_for_reconciliation(x402, monkeypatch) -> None:
    req = _request()
    quote, handle, receipt = _paid(x402)

    def boom_release(h, to):
        raise RuntimeError("chain reorg")

    monkeypatch.setattr(x402, "release", boom_release)
    with pytest.raises(AtomicLoopError, match="reconcile manually"):
        settle_with_atomic_citation(
            request=req,
            decision=_approved_decision(req),
            quote=quote,
            receipt=receipt,
            handle=handle,
            adapter=x402,
            handler=lambda r, q: {"ok": True},
            citation_writer=lambda r, o, ch: None,
            citation_hash="h",
        )


# ---------------------------------------------------------------------------
# Invariant: no intermediate state leaks
# ---------------------------------------------------------------------------
def test_concurrent_payer_payee_accounting_conservation(x402, settlement) -> None:
    """Sum of payer + payee balances is conserved across a full loop
    (neither creates nor destroys money, only moves it)."""
    payer_start = settlement.balance("payer", USDC)
    payee_start = settlement.balance("payee", USDC)
    total_start = (payer_start + payee_start).quantity

    req = _request()
    quote, handle, receipt = _paid(x402)
    settle_with_atomic_citation(
        request=req,
        decision=_approved_decision(req),
        quote=quote,
        receipt=receipt,
        handle=handle,
        adapter=x402,
        handler=lambda r, q: {"ok": True},
        citation_writer=lambda r, o, ch: None,
        citation_hash="h",
    )

    total_end = (
        settlement.balance("payer", USDC)
        + settlement.balance("payee", USDC)
    ).quantity
    assert total_start == total_end
