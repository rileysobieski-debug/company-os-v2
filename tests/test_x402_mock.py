"""X402MockAdapter tests: quote lifecycle, receipt verification, expiry."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from core.primitives.asset import AssetRef
from core.primitives.money import Money
from core.primitives.settlement_adapters.mock_adapter import MockSettlementAdapter
from core.settlement.x402_mock import (
    PaymentReceipt,
    X402MockAdapter,
    X402PaymentExpired,
    X402PaymentInsufficient,
    X402PaymentInvalid,
)


USDC = AssetRef(asset_id="USDC", decimals=6)


@pytest.fixture
def settlement() -> MockSettlementAdapter:
    adapter = MockSettlementAdapter(supported_assets=(USDC,))
    adapter.fund("payer", Money(Decimal("100"), USDC))
    return adapter


@pytest.fixture
def x402(settlement) -> X402MockAdapter:
    return X402MockAdapter(settlement)


# ---------------------------------------------------------------------------
# quote()
# ---------------------------------------------------------------------------
def test_quote_returns_payment_requirement(x402) -> None:
    req = x402.quote(
        resource_uri="/api/dispatch",
        amount=Money(Decimal("5"), USDC),
        payee_principal="payee",
    )
    assert req.resource_uri == "/api/dispatch"
    assert req.amount.quantity == Decimal("5")
    assert req.payee_principal == "payee"
    assert req.quote_id.startswith("q_")


def test_quote_records_expiry(x402) -> None:
    now = datetime(2026, 4, 24, 12, 0, 0, tzinfo=timezone.utc)
    req = x402.quote(
        resource_uri="/x",
        amount=Money(Decimal("1"), USDC),
        payee_principal="p",
        now=now,
    )
    assert "2026-04-24T12:05:00" in req.valid_until  # default 5-min TTL


def test_quote_rejects_unsupported_asset(x402) -> None:
    other = AssetRef(asset_id="BTC", decimals=8)
    with pytest.raises(X402PaymentInvalid):
        x402.quote(
            resource_uri="/x",
            amount=Money(Decimal("1"), other),
            payee_principal="p",
        )


def test_quote_is_idempotently_unique(x402) -> None:
    """Each call issues a fresh quote id; replaying a paid receipt
    against a new quote must fail (the nonce is the quote id)."""
    amount = Money(Decimal("1"), USDC)
    q1 = x402.quote(resource_uri="/x", amount=amount, payee_principal="p")
    q2 = x402.quote(resource_uri="/x", amount=amount, payee_principal="p")
    assert q1.quote_id != q2.quote_id


# ---------------------------------------------------------------------------
# lock_for_quote()
# ---------------------------------------------------------------------------
def test_lock_for_quote_issues_handle_and_receipt(x402) -> None:
    req = x402.quote(
        resource_uri="/x",
        amount=Money(Decimal("5"), USDC),
        payee_principal="payee",
    )
    handle, receipt = x402.lock_for_quote(req.quote_id, payer_principal="payer")
    assert handle.locked_amount.quantity == Decimal("5")
    assert receipt.quote_id == req.quote_id
    assert receipt.handle_id == handle.handle_id
    assert receipt.payer_principal == "payer"


def test_lock_for_quote_deducts_payer_balance(x402, settlement) -> None:
    before = settlement.balance("payer", USDC)
    req = x402.quote(
        resource_uri="/x",
        amount=Money(Decimal("10"), USDC),
        payee_principal="payee",
    )
    x402.lock_for_quote(req.quote_id, payer_principal="payer")
    after = settlement.balance("payer", USDC)
    assert (before - after).quantity == Decimal("10")


def test_lock_for_quote_unknown_quote_raises(x402) -> None:
    with pytest.raises(X402PaymentInvalid):
        x402.lock_for_quote("q_nonexistent", payer_principal="payer")


def test_lock_for_quote_expired_raises(x402) -> None:
    past = datetime.now(timezone.utc) - timedelta(minutes=10)
    req = x402.quote(
        resource_uri="/x",
        amount=Money(Decimal("1"), USDC),
        payee_principal="p",
        now=past,
    )
    with pytest.raises(X402PaymentExpired):
        x402.lock_for_quote(req.quote_id, payer_principal="payer")


def test_lock_for_quote_replay_rejected(x402) -> None:
    req = x402.quote(
        resource_uri="/x",
        amount=Money(Decimal("1"), USDC),
        payee_principal="p",
    )
    x402.lock_for_quote(req.quote_id, payer_principal="payer")
    # Same quote id is also the nonce on the underlying adapter;
    # second lock attempt must fail.
    with pytest.raises(Exception):  # EscrowStateError from underlying
        x402.lock_for_quote(req.quote_id, payer_principal="payer")


# ---------------------------------------------------------------------------
# verify_receipt()
# ---------------------------------------------------------------------------
def _paid(x402, *, amount_dec: Decimal = Decimal("5"), payer: str = "payer", payee: str = "payee", uri: str = "/x"):
    req = x402.quote(
        resource_uri=uri,
        amount=Money(amount_dec, USDC),
        payee_principal=payee,
    )
    handle, receipt = x402.lock_for_quote(req.quote_id, payer_principal=payer)
    return req, handle, receipt


def test_verify_receipt_happy_path(x402) -> None:
    req, _handle, receipt = _paid(x402)
    quote = x402.verify_receipt(receipt, expected_resource_uri=req.resource_uri)
    assert quote.quote_id == req.quote_id


def test_verify_receipt_wrong_resource_rejected(x402) -> None:
    _req, _handle, receipt = _paid(x402, uri="/a")
    with pytest.raises(X402PaymentInvalid):
        x402.verify_receipt(receipt, expected_resource_uri="/different")


def test_verify_receipt_unknown_quote_rejected(x402) -> None:
    bogus = PaymentReceipt(
        quote_id="q_not_issued",
        handle_id="h_whatever",
        payer_principal="payer",
        paid_amount=Money(Decimal("5"), USDC),
        signed_at="2026-04-24T00:00:00+00:00",
    )
    with pytest.raises(X402PaymentInvalid):
        x402.verify_receipt(bogus)


def test_verify_receipt_tampered_handle_rejected(x402) -> None:
    req, _handle, receipt = _paid(x402)
    tampered = PaymentReceipt(
        quote_id=receipt.quote_id,
        handle_id="h_forged",
        payer_principal=receipt.payer_principal,
        paid_amount=receipt.paid_amount,
        signed_at=receipt.signed_at,
    )
    with pytest.raises(X402PaymentInvalid):
        x402.verify_receipt(tampered)


def test_verify_receipt_insufficient_amount(x402) -> None:
    req, _handle, receipt = _paid(x402, amount_dec=Decimal("5"))
    short = PaymentReceipt(
        quote_id=receipt.quote_id,
        handle_id=receipt.handle_id,
        payer_principal=receipt.payer_principal,
        paid_amount=Money(Decimal("1"), USDC),
        signed_at=receipt.signed_at,
    )
    with pytest.raises(X402PaymentInsufficient):
        x402.verify_receipt(short)


def test_verify_receipt_after_release_rejected(x402) -> None:
    req, handle, receipt = _paid(x402)
    x402.release(handle, to="payee")
    with pytest.raises(X402PaymentInvalid):
        x402.verify_receipt(receipt)


# ---------------------------------------------------------------------------
# release / refund
# ---------------------------------------------------------------------------
def test_release_credits_payee(x402, settlement) -> None:
    _req, handle, _receipt = _paid(x402)
    before = settlement.balance("payee", USDC)
    x402.release(handle, to="payee")
    after = settlement.balance("payee", USDC)
    assert (after - before).quantity == Decimal("5")


def test_refund_credits_payer(x402, settlement) -> None:
    before = settlement.balance("payer", USDC)
    _req, handle, _receipt = _paid(x402)
    x402.refund(handle)
    after = settlement.balance("payer", USDC)
    assert before == after


def test_refund_unknown_handle_raises(x402) -> None:
    from core.primitives.settlement_adapters.base import EscrowHandle, EscrowHandleId

    bogus = EscrowHandle(
        handle_id=EscrowHandleId("h_ghost"),
        asset=USDC,
        locked_amount=Money(Decimal("1"), USDC),
        ref="/x",
    )
    with pytest.raises(X402PaymentInvalid):
        x402.refund(bogus)


# ---------------------------------------------------------------------------
# quote_fingerprint
# ---------------------------------------------------------------------------
def test_quote_fingerprint_is_sha256_hex(x402) -> None:
    req, _, _ = _paid(x402)
    fp = x402.quote_fingerprint(req)
    assert len(fp) == 64
    int(fp, 16)  # must parse as hex


def test_quote_fingerprint_is_stable(x402) -> None:
    req, _, _ = _paid(x402)
    assert x402.quote_fingerprint(req) == x402.quote_fingerprint(req)


def test_quote_fingerprint_is_content_addressed(x402) -> None:
    a, _, _ = _paid(x402, amount_dec=Decimal("1"))
    b, _, _ = _paid(x402, amount_dec=Decimal("2"))
    assert x402.quote_fingerprint(a) != x402.quote_fingerprint(b)
