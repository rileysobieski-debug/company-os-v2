"""tests/test_support_types.py — Ticket 0 support types + exception hierarchy.

`AssetRef` (Ticket 1) and `Money` (Ticket 2) don't exist yet, so the
tests here use minimal stub dataclasses that duck-type the interfaces
`EscrowHandle.to_dict` / `SettlementReceipt.to_dict` expect
(`.quantity` str-coercible, `.asset.asset_id` str-coercible).

Covered:
- Frozen dataclass equality + immutability (FrozenInstanceError).
- Hashability (usable in a set).
- JSON round-trip via canonical rules, with Decimal preserved exactly.
- `EscrowHandleId` NewType works with `uuid4().hex`.
- Full `SettlementError` subclass hierarchy: every subclass isinstance
  of the root.
"""
from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, dataclass
from decimal import Decimal
from uuid import uuid4

import pytest

from core.primitives.exceptions import (
    AdapterConflictError,
    AssetMismatchError,
    ChallengeError,
    EscrowStateError,
    InexactQuantizationError,
    SettlementError,
    SignatureError,
    UnsupportedAssetError,
    VerdictError,
)
from core.primitives.settlement_adapters import (
    EscrowHandle,
    EscrowHandleId,
    EscrowStatus,
    SettlementReceipt,
)


# ---------------------------------------------------------------------------
# Minimal duck-typed stubs for AssetRef and Money (pre-Ticket 1/2)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _StubAssetRef:
    asset_id: str
    decimals: int = 6


@dataclass(frozen=True)
class _StubMoney:
    quantity: Decimal
    asset: _StubAssetRef

    def to_dict(self) -> dict[str, str]:
        return {"quantity": str(self.quantity), "asset_id": self.asset.asset_id}


def _usdc() -> _StubAssetRef:
    return _StubAssetRef(asset_id="usdc", decimals=6)


def _money(qty: str, asset: _StubAssetRef | None = None) -> _StubMoney:
    return _StubMoney(quantity=Decimal(qty), asset=asset or _usdc())


def _new_handle_id() -> EscrowHandleId:
    # V0 rule: uuid4 hex. NewType is a runtime no-op — str passes fine.
    return EscrowHandleId(uuid4().hex)


# ---------------------------------------------------------------------------
# EscrowHandleId NewType
# ---------------------------------------------------------------------------
class TestEscrowHandleId:
    def test_newtype_wraps_str(self):
        hid = EscrowHandleId("abc123")
        # NewType is a runtime no-op — it's just the underlying type.
        assert hid == "abc123"
        assert isinstance(hid, str)

    def test_uuid4_hex_populates(self):
        hid = _new_handle_id()
        assert isinstance(hid, str)
        assert len(hid) == 32
        int(hid, 16)  # hex parseable


# ---------------------------------------------------------------------------
# EscrowStatus literal
# ---------------------------------------------------------------------------
class TestEscrowStatus:
    def test_literal_values_usable(self):
        # Literal is erased at runtime; the values are plain strings.
        for s in ("locked", "released", "slashed"):
            status: EscrowStatus = s  # type: ignore[assignment]
            assert status in {"locked", "released", "slashed"}


# ---------------------------------------------------------------------------
# EscrowHandle
# ---------------------------------------------------------------------------
class TestEscrowHandle:
    def _make(self, ref: str = "sla-001") -> EscrowHandle:
        return EscrowHandle(
            handle_id=_new_handle_id(),
            asset=_usdc(),  # type: ignore[arg-type]
            locked_amount=_money("12.345678"),  # type: ignore[arg-type]
            ref=ref,
        )

    def test_equality(self):
        hid = _new_handle_id()
        asset = _usdc()
        amt = _money("5.000000", asset)
        a = EscrowHandle(handle_id=hid, asset=asset, locked_amount=amt, ref="sla-x")  # type: ignore[arg-type]
        b = EscrowHandle(handle_id=hid, asset=asset, locked_amount=amt, ref="sla-x")  # type: ignore[arg-type]
        assert a == b
        assert hash(a) == hash(b)

    def test_frozen(self):
        h = self._make()
        with pytest.raises(FrozenInstanceError):
            h.ref = "mutated"  # type: ignore[misc]

    def test_hashable_in_set(self):
        h1 = self._make("sla-1")
        h2 = self._make("sla-2")
        s = {h1, h2, h1}
        assert len(s) == 2

    def test_to_dict_canonical(self):
        h = self._make()
        d = h.to_dict()
        assert set(d.keys()) == {"handle_id", "asset_id", "locked_amount", "ref"}
        assert d["locked_amount"] == {"quantity": "12.345678", "asset_id": "usdc"}
        # Canonical JSON is stable and does not use floats.
        raw = json.dumps(d, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        assert "12.345678" in raw
        assert "." not in raw.replace("12.345678", "")  # no stray floats

    def test_decimal_precision_preserved(self):
        # Decimal with many fractional digits must survive round-trip
        # with NO loss of precision. We compare via Decimal rather than
        # string form because `str(Decimal("0.000...001"))` canonicalizes
        # to `"1E-18"` — same value, different representation.
        qty = Decimal("0.000000000000000001")
        h = EscrowHandle(
            handle_id=_new_handle_id(),
            asset=_usdc(),  # type: ignore[arg-type]
            locked_amount=_StubMoney(quantity=qty, asset=_usdc()),  # type: ignore[arg-type]
            ref="tiny",
        )
        raw = json.dumps(h.to_dict(), sort_keys=True, separators=(",", ":"))
        reloaded = json.loads(raw)
        # No float anywhere — quantity is a string.
        assert isinstance(reloaded["locked_amount"]["quantity"], str)
        # Exact equality through Decimal round-trip.
        assert Decimal(reloaded["locked_amount"]["quantity"]) == qty
        # Also verify a user-friendly fixed-point form round-trips literally.
        fixed = "12.345678"
        h2 = EscrowHandle(
            handle_id=_new_handle_id(),
            asset=_usdc(),  # type: ignore[arg-type]
            locked_amount=_money(fixed),  # type: ignore[arg-type]
            ref="fixed",
        )
        raw2 = json.dumps(h2.to_dict(), sort_keys=True, separators=(",", ":"))
        reloaded2 = json.loads(raw2)
        assert reloaded2["locked_amount"]["quantity"] == fixed


# ---------------------------------------------------------------------------
# SettlementReceipt
# ---------------------------------------------------------------------------
class TestSettlementReceipt:
    def _released(self) -> SettlementReceipt:
        return SettlementReceipt(
            handle_id=_new_handle_id(),
            outcome="released",
            to="principal-42",
            transferred=_money("10.000000"),  # type: ignore[arg-type]
            burned=_money("0.000000"),  # type: ignore[arg-type]
            ts="2026-04-19T12:00:00Z",
        )

    def _slashed_fully_burned(self) -> SettlementReceipt:
        return SettlementReceipt(
            handle_id=_new_handle_id(),
            outcome="slashed",
            to="",
            transferred=_money("0.000000"),  # type: ignore[arg-type]
            burned=_money("10.000000"),  # type: ignore[arg-type]
            ts="2026-04-19T12:00:05Z",
        )

    def test_equality(self):
        hid = _new_handle_id()
        a = SettlementReceipt(
            handle_id=hid, outcome="released", to="p",
            transferred=_money("1"), burned=_money("0"),  # type: ignore[arg-type]
            ts="2026-04-19T12:00:00Z",
        )
        b = SettlementReceipt(
            handle_id=hid, outcome="released", to="p",
            transferred=_money("1"), burned=_money("0"),  # type: ignore[arg-type]
            ts="2026-04-19T12:00:00Z",
        )
        assert a == b
        assert hash(a) == hash(b)

    def test_frozen(self):
        r = self._released()
        with pytest.raises(FrozenInstanceError):
            r.to = "mutated"  # type: ignore[misc]

    def test_hashable_in_set(self):
        s = {self._released(), self._slashed_fully_burned()}
        assert len(s) == 2

    def test_to_dict_released_shape(self):
        d = self._released().to_dict()
        assert d["outcome"] == "released"
        assert d["to"] == "principal-42"
        assert d["transferred"] == {"quantity": "10.000000", "asset_id": "usdc"}
        assert d["burned"] == {"quantity": "0.000000", "asset_id": "usdc"}
        assert d["ts"] == "2026-04-19T12:00:00Z"

    def test_to_dict_fully_burned_shape(self):
        d = self._slashed_fully_burned().to_dict()
        assert d["outcome"] == "slashed"
        assert d["to"] == ""
        assert d["transferred"]["quantity"] == "0.000000"
        assert d["burned"]["quantity"] == "10.000000"

    def test_canonical_json_stable(self):
        r = self._released()
        raw1 = json.dumps(r.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        raw2 = json.dumps(r.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        assert raw1 == raw2
        # Round-trip preserves str-encoded decimals.
        reloaded = json.loads(raw1)
        assert Decimal(reloaded["transferred"]["quantity"]) == Decimal("10.000000")


# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------
class TestExceptionHierarchy:
    @pytest.mark.parametrize(
        "cls",
        [
            AssetMismatchError,
            UnsupportedAssetError,
            EscrowStateError,
            InexactQuantizationError,
            AdapterConflictError,
            SignatureError,
            VerdictError,
            ChallengeError,
        ],
    )
    def test_subclass_is_settlement_error(self, cls):
        assert issubclass(cls, SettlementError)
        assert isinstance(cls("boom"), SettlementError)
        # Also isinstance of built-in Exception so caller safety nets catch them.
        assert isinstance(cls("boom"), Exception)

    def test_settlement_error_catches_all(self):
        errors = [
            AssetMismatchError("a"),
            UnsupportedAssetError("b"),
            EscrowStateError("c"),
            InexactQuantizationError("d"),
            AdapterConflictError("e"),
            SignatureError("f"),
            VerdictError("g"),
            ChallengeError("h"),
        ]
        for e in errors:
            try:
                raise e
            except SettlementError as caught:
                assert caught is e

    def test_subclasses_are_distinct(self):
        # Each subclass must be its own type — no accidental aliasing.
        types = {
            SettlementError,
            AssetMismatchError,
            UnsupportedAssetError,
            EscrowStateError,
            InexactQuantizationError,
            AdapterConflictError,
            SignatureError,
            VerdictError,
            ChallengeError,
        }
        assert len(types) == 9
