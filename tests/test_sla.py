"""
tests/test_sla.py — Ticket 5 unit coverage for InterOrgSLA
==========================================================
Structural tests for `core.primitives.sla.InterOrgSLA`:

- canonical byte determinism across dict insertion order
- Money quantization stability in hash output
- timezone enforcement on `issued_at` / `expires_at`
- UTC normalization across aware datetimes + offset strings
- required-field validation
- signature presence does NOT affect `integrity_binding`
  (locks the Ticket 8 contract)
- `to_dict` / `from_dict` round-trip (with and without signatures)
- `to_dict` canonical-form expectations
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from core.primitives.asset import AssetRef, AssetRegistry
from core.primitives.exceptions import SignatureError
from core.primitives.identity import (
    Ed25519Keypair,
    Ed25519PublicKey,
    Signature,
)
from core.primitives.money import Money
from core.primitives.sla import InterOrgSLA, _canonical_bytes


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def usd() -> AssetRef:
    return AssetRef(asset_id="mock-usd", contract="USD", decimals=6)


@pytest.fixture
def eur() -> AssetRef:
    return AssetRef(asset_id="mock-eur", contract="EUR", decimals=2)


@pytest.fixture
def registry(usd: AssetRef, eur: AssetRef) -> AssetRegistry:
    """Hand-built registry so we don't depend on the on-disk YAML set."""
    reg = AssetRegistry()
    reg._assets[usd.asset_id] = usd  # type: ignore[attr-defined]
    reg._assets[eur.asset_id] = eur  # type: ignore[attr-defined]
    return reg


@pytest.fixture
def fake_signature() -> Signature:
    return Signature(
        sig_hex="a" * 128,
        signer=Ed25519PublicKey(bytes_hex="b" * 64),
    )


def _base_kwargs(usd: AssetRef) -> dict:
    """Helper — a fresh, valid kwargs bundle for `InterOrgSLA.create`."""
    return {
        "sla_id": "sla-001",
        "requester_node_did": "did:companyos:requester",
        "provider_node_did": "did:companyos:provider",
        "task_scope": "summarize 10-K",
        "deliverable_schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "risks": {"type": "array"},
            },
        },
        "accuracy_requirement": 0.9,
        "latency_ms": 120_000,
        "payment": Money(Decimal("10"), usd),
        "penalty_stake": Money(Decimal("2"), usd),
        "nonce": "0123456789abcdef0123456789abcdef",
        "issued_at": "2026-04-19T12:00:00Z",
        "expires_at": "2026-04-19T13:00:00Z",
    }


# ---------------------------------------------------------------------------
# Construction + validation
# ---------------------------------------------------------------------------
class TestConstruction:
    def test_create_returns_fully_populated_instance(self, usd, registry):
        sla = InterOrgSLA.create(**_base_kwargs(usd))
        assert sla.integrity_binding  # non-empty
        assert sla.protocol_version == "companyos-sla/0.1"
        assert sla.protocol_fee_bps == 0
        assert sla.requester_signature is None
        assert sla.provider_signature is None
        assert sla.verify_binding() is True

    def test_missing_nonce_raises(self, usd):
        kwargs = _base_kwargs(usd)
        del kwargs["nonce"]
        with pytest.raises(TypeError):
            InterOrgSLA.create(**kwargs)

    def test_empty_nonce_rejected(self, usd):
        kwargs = _base_kwargs(usd)
        kwargs["nonce"] = ""
        with pytest.raises(ValueError, match="nonce"):
            InterOrgSLA.create(**kwargs)

    def test_missing_issued_at_raises(self, usd):
        kwargs = _base_kwargs(usd)
        del kwargs["issued_at"]
        with pytest.raises(TypeError):
            InterOrgSLA.create(**kwargs)

    def test_new_nonce_is_32_char_hex(self):
        n = InterOrgSLA.new_nonce()
        assert len(n) == 32
        int(n, 16)  # doesn't raise


# ---------------------------------------------------------------------------
# Timestamp canonicalization
# ---------------------------------------------------------------------------
class TestTimestamps:
    def test_naive_datetime_rejected(self, usd):
        kwargs = _base_kwargs(usd)
        kwargs["issued_at"] = datetime(2026, 4, 19, 12, 0)
        with pytest.raises(ValueError, match="timezone-aware"):
            InterOrgSLA.create(**kwargs)

    def test_offset_free_string_rejected(self, usd):
        kwargs = _base_kwargs(usd)
        kwargs["issued_at"] = "2026-04-19T12:00:00"
        with pytest.raises(ValueError, match="timezone offset"):
            InterOrgSLA.create(**kwargs)

    def test_aware_datetime_accepted_and_normalized(self, usd):
        kwargs = _base_kwargs(usd)
        kwargs["issued_at"] = datetime(2026, 4, 19, 12, 0, tzinfo=timezone.utc)
        sla = InterOrgSLA.create(**kwargs)
        assert sla.issued_at == "2026-04-19T12:00:00Z"

    def test_offset_string_normalized_to_utc(self, usd):
        kwargs = _base_kwargs(usd)
        kwargs["issued_at"] = "2026-04-19T08:00:00-04:00"
        sla = InterOrgSLA.create(**kwargs)
        assert sla.issued_at == "2026-04-19T12:00:00Z"

    def test_non_utc_offset_and_z_form_hash_identically(self, usd):
        base = _base_kwargs(usd)
        base_a = dict(base, issued_at="2026-04-19T08:00:00-04:00")
        base_b = dict(base, issued_at="2026-04-19T12:00:00Z")
        sla_a = InterOrgSLA.create(**base_a)
        sla_b = InterOrgSLA.create(**base_b)
        assert sla_a.integrity_binding == sla_b.integrity_binding

    def test_sub_seconds_are_truncated(self, usd):
        kwargs = _base_kwargs(usd)
        kwargs["issued_at"] = "2026-04-19T12:00:00.987654+00:00"
        sla = InterOrgSLA.create(**kwargs)
        assert sla.issued_at == "2026-04-19T12:00:00Z"

    def test_bad_iso_string_raises(self, usd):
        kwargs = _base_kwargs(usd)
        kwargs["issued_at"] = "not a datetime"
        with pytest.raises(ValueError):
            InterOrgSLA.create(**kwargs)


# ---------------------------------------------------------------------------
# Canonical bytes determinism
# ---------------------------------------------------------------------------
class TestCanonicalBytes:
    def test_determinism_across_dict_insertion_order(self, usd):
        """Same deliverable_schema content, different insertion order →
        identical canonical bytes and therefore identical binding."""
        schema_a = {"type": "object", "properties": {"x": 1, "y": 2}}
        schema_b = {"properties": {"y": 2, "x": 1}, "type": "object"}

        kwargs_a = _base_kwargs(usd)
        kwargs_a["deliverable_schema"] = schema_a
        kwargs_b = _base_kwargs(usd)
        kwargs_b["deliverable_schema"] = schema_b

        sla_a = InterOrgSLA.create(**kwargs_a)
        sla_b = InterOrgSLA.create(**kwargs_b)
        assert sla_a.integrity_binding == sla_b.integrity_binding
        assert sla_a.canonical_bytes() == sla_b.canonical_bytes()

    def test_money_quantization_independence(self, usd):
        """Money(1) and Money('1.000000') hash identically — Money
        canonicalizes on construction, so the canonical bytes agree."""
        k1 = _base_kwargs(usd)
        k1["payment"] = Money(Decimal("1"), usd)
        k2 = _base_kwargs(usd)
        k2["payment"] = Money(Decimal("1.000000"), usd)

        sla_1 = InterOrgSLA.create(**k1)
        sla_2 = InterOrgSLA.create(**k2)
        assert sla_1.integrity_binding == sla_2.integrity_binding

    def test_binding_stable_across_runs(self, usd):
        """Same inputs → same binding every time."""
        kwargs = _base_kwargs(usd)
        bindings = {InterOrgSLA.create(**kwargs).integrity_binding for _ in range(5)}
        assert len(bindings) == 1

    def test_nonce_change_perturbs_binding(self, usd):
        k1 = _base_kwargs(usd)
        k2 = _base_kwargs(usd)
        k2["nonce"] = "ffffffffffffffffffffffffffffffff"
        sla_1 = InterOrgSLA.create(**k1)
        sla_2 = InterOrgSLA.create(**k2)
        assert sla_1.integrity_binding != sla_2.integrity_binding

    def test_canonical_bytes_excludes_signatures(self, usd, fake_signature):
        """Attaching signatures must NOT change the canonical byte output."""
        sla = InterOrgSLA.create(**_base_kwargs(usd))
        bytes_unsigned = sla.canonical_bytes()

        # Reconstruct a near-identical SLA with signatures populated.
        # Use the raw constructor because `create` always sets sigs to None.
        signed = InterOrgSLA(
            sla_id=sla.sla_id,
            requester_node_did=sla.requester_node_did,
            provider_node_did=sla.provider_node_did,
            task_scope=sla.task_scope,
            deliverable_schema=sla.deliverable_schema,
            accuracy_requirement=sla.accuracy_requirement,
            latency_ms=sla.latency_ms,
            payment=sla.payment,
            penalty_stake=sla.penalty_stake,
            nonce=sla.nonce,
            issued_at=sla.issued_at,
            expires_at=sla.expires_at,
            integrity_binding=sla.integrity_binding,
            protocol_version=sla.protocol_version,
            protocol_fee_bps=sla.protocol_fee_bps,
            requester_signature=fake_signature,
            provider_signature=fake_signature,
        )
        assert signed.canonical_bytes() == bytes_unsigned
        # And the binding should still verify against the canonical bytes.
        assert signed.verify_binding()

    def test_canonical_bytes_excludes_signatures_in_dict_mode(self, usd, fake_signature):
        """`_canonical_bytes` called on a dict shell must also strip signatures."""
        sla = InterOrgSLA.create(**_base_kwargs(usd))
        shell = sla.to_dict()
        bytes_from_clean = _canonical_bytes(
            {k: v for k, v in shell.items() if k not in {"requester_signature", "provider_signature"}}
        )
        shell["requester_signature"] = fake_signature.to_dict()
        bytes_from_dirty = _canonical_bytes(shell)
        assert bytes_from_clean == bytes_from_dirty


# ---------------------------------------------------------------------------
# Signature contract — signatures never change the binding
# ---------------------------------------------------------------------------
class TestSignatureContract:
    def test_attached_signature_does_not_change_binding(self, usd, fake_signature):
        sla = InterOrgSLA.create(**_base_kwargs(usd))
        original_binding = sla.integrity_binding

        with_sig = InterOrgSLA(
            sla_id=sla.sla_id,
            requester_node_did=sla.requester_node_did,
            provider_node_did=sla.provider_node_did,
            task_scope=sla.task_scope,
            deliverable_schema=sla.deliverable_schema,
            accuracy_requirement=sla.accuracy_requirement,
            latency_ms=sla.latency_ms,
            payment=sla.payment,
            penalty_stake=sla.penalty_stake,
            nonce=sla.nonce,
            issued_at=sla.issued_at,
            expires_at=sla.expires_at,
            integrity_binding=sla.integrity_binding,
            protocol_version=sla.protocol_version,
            protocol_fee_bps=sla.protocol_fee_bps,
            requester_signature=fake_signature,
        )
        assert with_sig.integrity_binding == original_binding
        assert with_sig.recompute_binding() == original_binding


# ---------------------------------------------------------------------------
# to_dict / from_dict round-trip
# ---------------------------------------------------------------------------
class TestRoundTrip:
    def test_to_dict_shape(self, usd):
        sla = InterOrgSLA.create(**_base_kwargs(usd))
        d = sla.to_dict()
        # Money payloads are dicts of strings.
        assert d["payment"] == {"quantity": "10.000000", "asset_id": "mock-usd"}
        assert d["penalty_stake"] == {"quantity": "2.000000", "asset_id": "mock-usd"}
        assert d["integrity_binding"] == sla.integrity_binding
        assert d["requester_signature"] is None
        assert d["provider_signature"] is None

    def test_to_dict_is_json_roundtrip_stable(self, usd):
        """`to_dict` output serializes to JSON cleanly with sorted keys."""
        sla = InterOrgSLA.create(**_base_kwargs(usd))
        d = sla.to_dict()
        encoded = json.dumps(d, sort_keys=True, separators=(",", ":"))
        decoded = json.loads(encoded)
        assert decoded["sla_id"] == "sla-001"

    def test_round_trip_without_signatures(self, usd, registry):
        original = InterOrgSLA.create(**_base_kwargs(usd))
        rehydrated = InterOrgSLA.from_dict(original.to_dict(), registry)
        assert rehydrated == original
        assert rehydrated.verify_binding()

    def test_round_trip_with_signatures(self, usd, registry, fake_signature):
        base = InterOrgSLA.create(**_base_kwargs(usd))
        signed = InterOrgSLA(
            sla_id=base.sla_id,
            requester_node_did=base.requester_node_did,
            provider_node_did=base.provider_node_did,
            task_scope=base.task_scope,
            deliverable_schema=base.deliverable_schema,
            accuracy_requirement=base.accuracy_requirement,
            latency_ms=base.latency_ms,
            payment=base.payment,
            penalty_stake=base.penalty_stake,
            nonce=base.nonce,
            issued_at=base.issued_at,
            expires_at=base.expires_at,
            integrity_binding=base.integrity_binding,
            protocol_version=base.protocol_version,
            protocol_fee_bps=base.protocol_fee_bps,
            requester_signature=fake_signature,
            provider_signature=fake_signature,
        )
        rehydrated = InterOrgSLA.from_dict(signed.to_dict(), registry)
        assert rehydrated.requester_signature == fake_signature
        assert rehydrated.provider_signature == fake_signature
        assert rehydrated.integrity_binding == base.integrity_binding
        assert rehydrated.verify_binding()

    def test_round_trip_resolves_cross_asset_money(self, registry, usd, eur):
        """Payment in USD, penalty in EUR — from_dict must route each
        Money through the registry independently."""
        kwargs = _base_kwargs(usd)
        kwargs["penalty_stake"] = Money(Decimal("5.00"), eur)
        original = InterOrgSLA.create(**kwargs)

        rehydrated = InterOrgSLA.from_dict(original.to_dict(), registry)
        assert rehydrated.payment.asset.asset_id == "mock-usd"
        assert rehydrated.penalty_stake.asset.asset_id == "mock-eur"
        assert rehydrated == original

    def test_from_dict_missing_field_raises(self, usd, registry):
        d = InterOrgSLA.create(**_base_kwargs(usd)).to_dict()
        del d["sla_id"]
        with pytest.raises(ValueError, match="sla_id"):
            InterOrgSLA.from_dict(d, registry)

    def test_from_dict_preserves_stored_binding_even_if_tampered(
        self, usd, registry
    ):
        """If someone mutates task_scope on disk without rewriting the
        binding, `from_dict` must accept the stored binding verbatim and
        rely on `verify_binding()` to catch the tamper. This is the same
        contract integrity.py enforces."""
        d = InterOrgSLA.create(**_base_kwargs(usd)).to_dict()
        d["task_scope"] = "tampered"
        rehydrated = InterOrgSLA.from_dict(d, registry)
        assert rehydrated.verify_binding() is False


# ---------------------------------------------------------------------------
# Protocol fields
# ---------------------------------------------------------------------------
class TestProtocolFields:
    def test_protocol_fee_bps_participates_in_binding(self, usd):
        k1 = _base_kwargs(usd)
        k2 = _base_kwargs(usd)
        k2["protocol_fee_bps"] = 25
        sla_1 = InterOrgSLA.create(**k1)
        sla_2 = InterOrgSLA.create(**k2)
        assert sla_1.integrity_binding != sla_2.integrity_binding

    def test_non_default_protocol_version_tracked(self, usd):
        kwargs = _base_kwargs(usd)
        kwargs["protocol_version"] = "companyos-sla/0.2"
        sla = InterOrgSLA.create(**kwargs)
        assert sla.protocol_version == "companyos-sla/0.2"

    def test_negative_protocol_fee_rejected(self, usd):
        kwargs = _base_kwargs(usd)
        kwargs["protocol_fee_bps"] = -1
        with pytest.raises(ValueError):
            InterOrgSLA.create(**kwargs)

    def test_negative_latency_rejected(self, usd):
        kwargs = _base_kwargs(usd)
        kwargs["latency_ms"] = -100
        with pytest.raises(ValueError):
            InterOrgSLA.create(**kwargs)


# ---------------------------------------------------------------------------
# Ticket 8 — signing + verification
# ---------------------------------------------------------------------------
class TestSigning:
    def test_sign_as_requester_populates_only_requester(self, usd):
        sla = InterOrgSLA.create(**_base_kwargs(usd))
        kp = Ed25519Keypair.generate()
        signed = sla.sign_as_requester(kp)
        assert signed.requester_signature is not None
        assert signed.provider_signature is None
        assert signed.requester_signature.signer == kp.public_key

    def test_sign_as_provider_populates_only_provider(self, usd):
        sla = InterOrgSLA.create(**_base_kwargs(usd))
        kp = Ed25519Keypair.generate()
        signed = sla.sign_as_provider(kp)
        assert signed.provider_signature is not None
        assert signed.requester_signature is None
        assert signed.provider_signature.signer == kp.public_key

    def test_verify_signatures_happy_path(self, usd):
        sla = InterOrgSLA.create(**_base_kwargs(usd))
        req_kp = Ed25519Keypair.generate()
        prov_kp = Ed25519Keypair.generate()
        signed = sla.sign_as_requester(req_kp).sign_as_provider(prov_kp)
        # Should not raise.
        signed.verify_signatures(
            requester_pubkey=req_kp.public_key,
            provider_pubkey=prov_kp.public_key,
        )

    def test_signature_round_trip_through_dict(self, usd, registry):
        """Sign both sides → to_dict → from_dict → verify still passes."""
        sla = InterOrgSLA.create(**_base_kwargs(usd))
        req_kp = Ed25519Keypair.generate()
        prov_kp = Ed25519Keypair.generate()
        signed = sla.sign_as_requester(req_kp).sign_as_provider(prov_kp)

        rehydrated = InterOrgSLA.from_dict(signed.to_dict(), registry)
        assert rehydrated == signed
        rehydrated.verify_signatures(
            requester_pubkey=req_kp.public_key,
            provider_pubkey=prov_kp.public_key,
        )

    def test_signing_order_is_immaterial(self, usd):
        """Requester-first and provider-first must yield identical SLAs.

        This locks the Ticket 5 canonical-exclusion contract: both
        signature fields are excluded from canonical bytes, so neither
        signer's input depends on the other's signature state.
        """
        sla = InterOrgSLA.create(**_base_kwargs(usd))
        req_kp = Ed25519Keypair.generate()
        prov_kp = Ed25519Keypair.generate()

        req_first = sla.sign_as_requester(req_kp).sign_as_provider(prov_kp)
        prov_first = sla.sign_as_provider(prov_kp).sign_as_requester(req_kp)

        assert req_first.requester_signature == prov_first.requester_signature
        assert req_first.provider_signature == prov_first.provider_signature

    def test_different_keypairs_yield_same_integrity_binding(self, usd):
        """Two SLAs with identical terms but different signing keypairs
        MUST share an `integrity_binding` — signatures are outside the
        hashed body by construction."""
        sla = InterOrgSLA.create(**_base_kwargs(usd))
        kp_a_req = Ed25519Keypair.generate()
        kp_a_prov = Ed25519Keypair.generate()
        kp_b_req = Ed25519Keypair.generate()
        kp_b_prov = Ed25519Keypair.generate()

        signed_a = sla.sign_as_requester(kp_a_req).sign_as_provider(kp_a_prov)
        signed_b = sla.sign_as_requester(kp_b_req).sign_as_provider(kp_b_prov)

        assert signed_a.integrity_binding == signed_b.integrity_binding
        assert signed_a.integrity_binding == sla.integrity_binding

    def test_tamper_after_signing_breaks_verification(self, usd):
        """Sign an SLA → mutate a field via dataclasses.replace →
        signatures over the original bytes no longer validate."""
        import dataclasses
        from decimal import Decimal

        sla = InterOrgSLA.create(**_base_kwargs(usd))
        req_kp = Ed25519Keypair.generate()
        prov_kp = Ed25519Keypair.generate()
        signed = sla.sign_as_requester(req_kp).sign_as_provider(prov_kp)

        # Tamper the payment quantity. The binding is left stale on
        # purpose — we're simulating wire tamper, not a re-issuance.
        tampered = dataclasses.replace(
            signed, payment=Money(Decimal("9999"), signed.payment.asset)
        )
        with pytest.raises(SignatureError, match="cryptographic verification"):
            tampered.verify_signatures(
                requester_pubkey=req_kp.public_key,
                provider_pubkey=prov_kp.public_key,
            )

    def test_wrong_signer_detected(self, usd):
        """Signing as provider with the requester's keypair fails
        verification because the embedded signer pubkey mismatches the
        expected provider pubkey — caught before any crypto check."""
        sla = InterOrgSLA.create(**_base_kwargs(usd))
        req_kp = Ed25519Keypair.generate()
        prov_kp = Ed25519Keypair.generate()

        # Provider slot signed by requester's keypair by mistake.
        bogus = sla.sign_as_requester(req_kp).sign_as_provider(req_kp)

        with pytest.raises(SignatureError, match="Provider signature signer"):
            bogus.verify_signatures(
                requester_pubkey=req_kp.public_key,
                provider_pubkey=prov_kp.public_key,
            )

    def test_missing_requester_signature_raises(self, usd):
        sla = InterOrgSLA.create(**_base_kwargs(usd))
        prov_kp = Ed25519Keypair.generate()
        req_kp = Ed25519Keypair.generate()
        half = sla.sign_as_provider(prov_kp)
        with pytest.raises(SignatureError, match="Requester signature missing"):
            half.verify_signatures(
                requester_pubkey=req_kp.public_key,
                provider_pubkey=prov_kp.public_key,
            )

    def test_missing_provider_signature_raises(self, usd):
        sla = InterOrgSLA.create(**_base_kwargs(usd))
        req_kp = Ed25519Keypair.generate()
        prov_kp = Ed25519Keypair.generate()
        half = sla.sign_as_requester(req_kp)
        with pytest.raises(SignatureError, match="Provider signature missing"):
            half.verify_signatures(
                requester_pubkey=req_kp.public_key,
                provider_pubkey=prov_kp.public_key,
            )

    def test_verify_signatures_requires_mode(self, usd):
        """No registry AND no explicit pubkeys → TypeError (avoids
        silently passing verification on an under-specified call)."""
        sla = InterOrgSLA.create(**_base_kwargs(usd))
        req_kp = Ed25519Keypair.generate()
        prov_kp = Ed25519Keypair.generate()
        signed = sla.sign_as_requester(req_kp).sign_as_provider(prov_kp)
        with pytest.raises(TypeError):
            signed.verify_signatures()

    def test_verify_signatures_rejects_partial_pubkeys(self, usd):
        sla = InterOrgSLA.create(**_base_kwargs(usd))
        req_kp = Ed25519Keypair.generate()
        prov_kp = Ed25519Keypair.generate()
        signed = sla.sign_as_requester(req_kp).sign_as_provider(prov_kp)
        with pytest.raises(TypeError):
            signed.verify_signatures(requester_pubkey=req_kp.public_key)

    def test_verify_signatures_rejects_mixed_modes(self, usd):
        sla = InterOrgSLA.create(**_base_kwargs(usd))
        req_kp = Ed25519Keypair.generate()
        prov_kp = Ed25519Keypair.generate()
        signed = sla.sign_as_requester(req_kp).sign_as_provider(prov_kp)
        with pytest.raises(TypeError):
            signed.verify_signatures(
                registry=object(),
                requester_pubkey=req_kp.public_key,
                provider_pubkey=prov_kp.public_key,
            )

    def test_verify_signatures_registry_mode_wired(self, usd):
        """Ticket 10 wires up registry mode — passing a registry that
        maps the requester/provider DIDs to their signing pubkeys now
        succeeds where Ticket 8 raised NotImplementedError."""
        from core.primitives.node_registry import NodeRegistry

        sla = InterOrgSLA.create(**_base_kwargs(usd))
        req_kp = Ed25519Keypair.generate()
        prov_kp = Ed25519Keypair.generate()
        signed = sla.sign_as_requester(req_kp).sign_as_provider(prov_kp)

        reg = NodeRegistry()
        # Prime directly via the internal dict — register() needs a
        # root Path, and this test only exercises the lookup path.
        reg._nodes[sla.requester_node_did] = {  # type: ignore[attr-defined]
            "public_key_hex": req_kp.public_key.bytes_hex,
            "first_seen": "",
            "notes": "",
        }
        reg._nodes[sla.provider_node_did] = {  # type: ignore[attr-defined]
            "public_key_hex": prov_kp.public_key.bytes_hex,
            "first_seen": "",
            "notes": "",
        }
        # Should not raise.
        signed.verify_signatures(registry=reg)


# ---------------------------------------------------------------------------
# v1a oracle fields (Ticket A3)
# ---------------------------------------------------------------------------
class TestV1aOracleFields:
    """Tests for the four oracle-related fields added in v1a:
    `artifact_hash_at_delivery`, `primary_evaluator_did`,
    `canonical_evaluator_hash`, and `challenge_window_sec`.
    """

    def test_defaults_populated_on_create(self, usd):
        sla = InterOrgSLA.create(**_base_kwargs(usd))
        assert sla.artifact_hash_at_delivery == ""
        assert sla.primary_evaluator_did is None
        assert sla.canonical_evaluator_hash is None
        assert sla.challenge_window_sec == 86_400

    def test_artifact_hash_roundtrips_through_dict(self, usd, registry):
        sla = InterOrgSLA.create(**_base_kwargs(usd))
        populated = sla.with_delivery_hash("deadbeef" * 8)
        rehydrated = InterOrgSLA.from_dict(populated.to_dict(), registry)
        assert rehydrated.artifact_hash_at_delivery == "deadbeef" * 8
        assert rehydrated == populated

    def test_v1b_reserved_fields_roundtrip(self, usd, registry):
        kwargs = _base_kwargs(usd)
        kwargs["primary_evaluator_did"] = "did:companyos:judge-001"
        kwargs["canonical_evaluator_hash"] = "a" * 64
        kwargs["challenge_window_sec"] = 3_600
        sla = InterOrgSLA.create(**kwargs)
        assert sla.primary_evaluator_did == "did:companyos:judge-001"
        assert sla.canonical_evaluator_hash == "a" * 64
        assert sla.challenge_window_sec == 3_600

        rehydrated = InterOrgSLA.from_dict(sla.to_dict(), registry)
        assert rehydrated.primary_evaluator_did == "did:companyos:judge-001"
        assert rehydrated.canonical_evaluator_hash == "a" * 64
        assert rehydrated.challenge_window_sec == 3_600

    def test_v1b_fields_change_integrity_binding(self, usd):
        """Adding primary_evaluator_did or canonical_evaluator_hash to an
        otherwise identical SLA deterministically changes the binding.
        Spec §A3: 'Adding ... changes integrity_binding (tested).'"""
        plain = InterOrgSLA.create(**_base_kwargs(usd))

        with_pe = InterOrgSLA.create(
            **dict(_base_kwargs(usd), primary_evaluator_did="did:x")
        )
        with_ch = InterOrgSLA.create(
            **dict(_base_kwargs(usd), canonical_evaluator_hash="b" * 64)
        )
        with_cw = InterOrgSLA.create(
            **dict(_base_kwargs(usd), challenge_window_sec=7_200)
        )

        # Each variant must differ from plain and from each other.
        bindings = {
            plain.integrity_binding,
            with_pe.integrity_binding,
            with_ch.integrity_binding,
            with_cw.integrity_binding,
        }
        assert len(bindings) == 4

        # Deterministic: recreate with_pe with the same inputs, same binding.
        with_pe_again = InterOrgSLA.create(
            **dict(_base_kwargs(usd), primary_evaluator_did="did:x")
        )
        assert with_pe_again.integrity_binding == with_pe.integrity_binding

    def test_v1a_created_sla_self_verifies(self, usd):
        """A fresh v1a SLA (with all new-field defaults) must verify its
        own binding so future canonical-shape changes stay caught."""
        sla = InterOrgSLA.create(**_base_kwargs(usd))
        assert sla.verify_binding() is True

    def test_challenge_window_below_floor_raises(self, usd):
        kwargs = _base_kwargs(usd)
        kwargs["challenge_window_sec"] = 59
        with pytest.raises(ValueError, match="challenge_window_sec"):
            InterOrgSLA.create(**kwargs)

    def test_challenge_window_above_ceiling_raises(self, usd):
        kwargs = _base_kwargs(usd)
        kwargs["challenge_window_sec"] = 604_801
        with pytest.raises(ValueError, match="challenge_window_sec"):
            InterOrgSLA.create(**kwargs)

    def test_challenge_window_boundaries_accepted(self, usd):
        """60s (floor) and 604_800s (7-day ceiling) must both construct."""
        low = InterOrgSLA.create(
            **dict(_base_kwargs(usd), challenge_window_sec=60)
        )
        high = InterOrgSLA.create(
            **dict(_base_kwargs(usd), challenge_window_sec=604_800)
        )
        assert low.challenge_window_sec == 60
        assert high.challenge_window_sec == 604_800

    def test_challenge_window_wrong_type_raises(self, usd):
        kwargs = _base_kwargs(usd)
        kwargs["challenge_window_sec"] = 3600.0
        with pytest.raises(TypeError, match="challenge_window_sec"):
            InterOrgSLA.create(**kwargs)

    def test_challenge_window_bool_rejected(self, usd):
        """bool is an int subclass; reject explicitly so True/False don't
        silently sneak past the range check."""
        kwargs = _base_kwargs(usd)
        kwargs["challenge_window_sec"] = True
        with pytest.raises(TypeError, match="challenge_window_sec"):
            InterOrgSLA.create(**kwargs)

    def test_artifact_hash_not_string_raises(self, usd):
        kwargs = _base_kwargs(usd)
        kwargs["artifact_hash_at_delivery"] = None
        with pytest.raises(TypeError, match="artifact_hash_at_delivery"):
            InterOrgSLA.create(**kwargs)

    def test_with_delivery_hash_populates_and_rebinds(self, usd):
        sla = InterOrgSLA.create(**_base_kwargs(usd))
        assert sla.artifact_hash_at_delivery == ""
        original_binding = sla.integrity_binding

        delivered = sla.with_delivery_hash("cafebabe" * 8)
        assert delivered.artifact_hash_at_delivery == "cafebabe" * 8
        assert delivered.integrity_binding != original_binding
        assert delivered.verify_binding() is True

    def test_with_delivery_hash_is_pure(self, usd):
        """Returns a new instance; original SLA unchanged."""
        sla = InterOrgSLA.create(**_base_kwargs(usd))
        _ = sla.with_delivery_hash("f" * 64)
        assert sla.artifact_hash_at_delivery == ""

    def test_with_delivery_hash_rejects_empty(self, usd):
        sla = InterOrgSLA.create(**_base_kwargs(usd))
        with pytest.raises(ValueError, match="artifact_hash"):
            sla.with_delivery_hash("")

    def test_with_delivery_hash_rejects_non_string(self, usd):
        sla = InterOrgSLA.create(**_base_kwargs(usd))
        with pytest.raises(ValueError, match="artifact_hash"):
            sla.with_delivery_hash(0xDEADBEEF)  # type: ignore[arg-type]

    def test_primary_evaluator_did_empty_string_rejected(self, usd):
        kwargs = _base_kwargs(usd)
        kwargs["primary_evaluator_did"] = ""
        with pytest.raises(ValueError, match="primary_evaluator_did"):
            InterOrgSLA.create(**kwargs)

    def test_canonical_evaluator_hash_empty_string_rejected(self, usd):
        kwargs = _base_kwargs(usd)
        kwargs["canonical_evaluator_hash"] = ""
        with pytest.raises(ValueError, match="canonical_evaluator_hash"):
            InterOrgSLA.create(**kwargs)

    def test_from_dict_defaults_for_missing_v1a_fields(self, usd, registry):
        """A payload that pre-dates v1a and omits the four new fields
        still rehydrates; defaults fill in. This is what lets an old
        serialized SLA still load under the new schema.
        """
        sla = InterOrgSLA.create(**_base_kwargs(usd))
        payload = sla.to_dict()
        for key in (
            "artifact_hash_at_delivery",
            "primary_evaluator_did",
            "canonical_evaluator_hash",
            "challenge_window_sec",
        ):
            payload.pop(key, None)
        rehydrated = InterOrgSLA.from_dict(payload, registry)
        assert rehydrated.artifact_hash_at_delivery == ""
        assert rehydrated.primary_evaluator_did is None
        assert rehydrated.canonical_evaluator_hash is None
        assert rehydrated.challenge_window_sec == 86_400
