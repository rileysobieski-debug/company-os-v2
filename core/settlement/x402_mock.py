"""x402-shaped mock settlement adapter.

x402 is the specification that reclaims HTTP 402 (Payment Required) as
a live status code. A server that wants money for a resource returns
402 with a `PaymentRequirement`; the client fulfills via any compatible
wallet, attaches a `PaymentReceipt`, and retries. The chassis uses
this shape as the agent-facing contract for paid actions:

    - Agent requests a paid action (spend_commit, external API call).
    - PVE evaluator issues APPROVE (the policy gate).
    - X402MockAdapter.quote(resource_uri) issues a PaymentRequirement
      naming the asset, amount, payee, and a valid_until deadline.
    - Caller locks an escrow on the underlying settlement primitive
      (MockSettlementAdapter or a real Coinbase Agentic Wallet in v6
      Weeks 9-12) referencing the quote id.
    - X402MockAdapter.verify_receipt(receipt) validates the receipt
      against the quote and the settlement handle.
    - atomic_loop.settle_with_atomic_citation runs the handler,
      releases the escrow, and writes the citation as one unit.

The mock is NOT the Coinbase Agentic Wallet. It models the SHAPE of
the protocol so the composition layer above it (atomic_loop) is the
same against mock and real wallet. Swapping to a real wallet replaces
the adapter instance; the atomic loop does not change.
"""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

from core.primitives.asset import AssetRef
from core.primitives.money import Money
from core.primitives.settlement_adapters.base import (
    EscrowHandle,
    EscrowStatus,
    SettlementReceipt,
)
from core.primitives.settlement_adapters.mock_adapter import MockSettlementAdapter


class X402Error(RuntimeError):
    """Base class for x402-specific failures."""


class X402PaymentExpired(X402Error):
    """The PaymentRequirement's `valid_until` is in the past."""


class X402PaymentInsufficient(X402Error):
    """The locked escrow amount is less than the quoted amount."""


class X402PaymentInvalid(X402Error):
    """The receipt's quote id does not match any issued quote."""


@dataclass(frozen=True)
class PaymentRequirement:
    """x402 server -> client payment descriptor.

    Shape mirrors the fields every x402 adapter must emit regardless of
    wallet / chain: a quote id, the resource being priced, the exact
    amount, the payee principal, the asset, and an expiry.
    """
    quote_id: str
    resource_uri: str
    amount: Money
    payee_principal: str
    asset: AssetRef
    valid_until: str  # ISO-8601 UTC
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["amount"] = self.amount.to_dict()
        d["asset"] = {"asset_id": self.asset.asset_id, "decimals": self.asset.decimals}
        return d


@dataclass(frozen=True)
class PaymentReceipt:
    """x402 client -> server payment proof. Returned to the caller so
    they can reattach it on the retry that actually delivers the
    resource. References the settlement handle that locked the funds."""
    quote_id: str
    handle_id: str
    payer_principal: str
    paid_amount: Money
    signed_at: str

    def to_dict(self) -> dict:
        d = asdict(self)
        d["paid_amount"] = self.paid_amount.to_dict()
        return d


class X402MockAdapter:
    """In-process x402 adapter. Wraps a MockSettlementAdapter for the
    actual escrow mechanics; adds x402 protocol state (quote issuance,
    expiry, receipt validation).

    Concurrency: single-process, single-threaded, matching the
    underlying MockSettlementAdapter. External synchronization is the
    caller's responsibility in a multi-thread deployment.
    """

    DEFAULT_QUOTE_TTL = timedelta(minutes=5)

    def __init__(
        self,
        settlement: MockSettlementAdapter,
        *,
        quote_ttl: timedelta | None = None,
    ) -> None:
        self._settlement = settlement
        self._ttl = quote_ttl or self.DEFAULT_QUOTE_TTL
        self._quotes: dict[str, PaymentRequirement] = {}
        self._handles_by_quote: dict[str, str] = {}

    @property
    def settlement(self) -> MockSettlementAdapter:
        return self._settlement

    # ---- quote / verify -------------------------------------------------
    def quote(
        self,
        *,
        resource_uri: str,
        amount: Money,
        payee_principal: str,
        now: datetime | None = None,
    ) -> PaymentRequirement:
        """Issue a PaymentRequirement for `resource_uri` priced at
        `amount`. Returned object is what the server sends back with
        HTTP 402 `X-Payment-Requirement` header in a real deployment."""
        now = now or datetime.now(timezone.utc)
        if not self._settlement.supports(amount.asset):
            raise X402PaymentInvalid(
                f"settlement adapter does not support asset "
                f"{amount.asset.asset_id!r}",
            )
        quote_id = f"q_{uuid.uuid4().hex}"
        req = PaymentRequirement(
            quote_id=quote_id,
            resource_uri=resource_uri,
            amount=amount,
            payee_principal=payee_principal,
            asset=amount.asset,
            valid_until=(now + self._ttl).isoformat(),
        )
        self._quotes[quote_id] = req
        return req

    def lock_for_quote(
        self,
        quote_id: str,
        *,
        payer_principal: str,
        now: datetime | None = None,
    ) -> tuple[EscrowHandle, PaymentReceipt]:
        """Caller pattern: `quote` then `lock_for_quote`. Combines the
        escrow lock with a receipt so downstream atomic_loop calls can
        present both in one step."""
        now = now or datetime.now(timezone.utc)
        quote = self._quotes.get(quote_id)
        if quote is None:
            raise X402PaymentInvalid(f"unknown quote id {quote_id!r}")
        if _parse_iso(quote.valid_until) < now:
            raise X402PaymentExpired(
                f"quote {quote_id} expired at {quote.valid_until}",
            )
        handle = self._settlement.lock(
            quote.amount,
            ref=quote.resource_uri,
            nonce=quote_id,
            principal=payer_principal,
        )
        self._handles_by_quote[quote_id] = handle.handle_id
        receipt = PaymentReceipt(
            quote_id=quote_id,
            handle_id=handle.handle_id,
            payer_principal=payer_principal,
            paid_amount=quote.amount,
            signed_at=now.isoformat(),
        )
        return handle, receipt

    def verify_receipt(
        self,
        receipt: PaymentReceipt,
        *,
        expected_resource_uri: str | None = None,
        now: datetime | None = None,
    ) -> PaymentRequirement:
        """Server-side validation: the receipt must reference a known
        quote whose handle matches and is still locked. Optional
        `expected_resource_uri` guards against a receipt being reused
        for a different resource."""
        now = now or datetime.now(timezone.utc)
        quote = self._quotes.get(receipt.quote_id)
        if quote is None:
            raise X402PaymentInvalid(
                f"receipt references unknown quote {receipt.quote_id!r}",
            )
        if expected_resource_uri and quote.resource_uri != expected_resource_uri:
            raise X402PaymentInvalid(
                f"receipt's quote is for resource {quote.resource_uri!r}, "
                f"not {expected_resource_uri!r}",
            )
        if self._handles_by_quote.get(receipt.quote_id) != receipt.handle_id:
            raise X402PaymentInvalid(
                f"receipt handle {receipt.handle_id!r} does not match quote's "
                "locked handle",
            )
        handle_id = receipt.handle_id
        status_record = self._settlement.escrows.get(handle_id)
        if status_record is None:
            raise X402PaymentInvalid(
                f"settlement handle {handle_id!r} unknown",
            )
        if status_record.status != "locked":
            raise X402PaymentInvalid(
                f"handle {handle_id!r} is not locked (status={status_record.status})",
            )
        if receipt.paid_amount.quantity < quote.amount.quantity:
            raise X402PaymentInsufficient(
                f"receipt paid {receipt.paid_amount.to_dict()}, "
                f"quote requires {quote.amount.to_dict()}",
            )
        return quote

    # ---- direct settlement surface --------------------------------------
    def release(
        self,
        handle: EscrowHandle,
        *,
        to: str,
    ) -> SettlementReceipt:
        """Pass-through to the underlying adapter. Kept here so callers
        never need to reach past the x402 layer."""
        return self._settlement.release(handle, to)

    def refund(self, handle: EscrowHandle) -> SettlementReceipt:
        """Convenience: release back to the original locker. 'Refund'
        is the semantic name when the resource was not delivered."""
        record = self._settlement.escrows.get(handle.handle_id)
        if record is None:
            raise X402PaymentInvalid(f"handle {handle.handle_id!r} unknown")
        return self._settlement.release(handle, record.locker)

    def status_of(self, handle: EscrowHandle) -> EscrowStatus:
        return self._settlement.get_status(handle)

    # ---- observability --------------------------------------------------
    def iter_quotes(self):
        yield from self._quotes.values()

    def quote_fingerprint(self, quote: PaymentRequirement) -> str:
        """SHA-256 over the canonical quote bytes. Useful for citation
        provenance: the citation can reference this fingerprint instead
        of the raw quote."""
        import json
        payload = json.dumps(quote.to_dict(), sort_keys=True).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


def _parse_iso(ts: str) -> datetime:
    s = ts.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


__all__ = [
    "PaymentReceipt",
    "PaymentRequirement",
    "X402Error",
    "X402MockAdapter",
    "X402PaymentExpired",
    "X402PaymentInsufficient",
    "X402PaymentInvalid",
]
