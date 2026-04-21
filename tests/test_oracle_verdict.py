"""
tests/test_oracle_verdict.py -- Ticket A2 coverage for OracleVerdict
=====================================================================
Unit tests for `core.primitives.oracle.OracleVerdict`:

- Canonical determinism (dict insertion order, nested evidence dicts)
- Signature round-trip: create -> to_dict -> from_dict -> verify_signature
- Tamper detection: mutate evidence after signing raises SignatureError
- Null score serializes as explicit null, not absent
- Unknown evidence kind on from_dict raises VerdictError
- Signer/signature consistency enforced at construction
- verdict_hash stability across independent constructions with same inputs
- Tier and result Literal values validated
- Protocol version default populated
"""
from __future__ import annotations

import dataclasses
import json
from decimal import Decimal

import pytest

from core.primitives.exceptions import SignatureError, VerdictError
from core.primitives.identity import (
    Ed25519Keypair,
    Ed25519PublicKey,
    Signature,
)
from core.primitives.oracle import (
    OracleVerdict,
    _canonical_bytes,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def keypair() -> Ed25519Keypair:
    return Ed25519Keypair.generate()


@pytest.fixture
def other_keypair() -> Ed25519Keypair:
    return Ed25519Keypair.generate()


def _base_kwargs(keypair: Ed25519Keypair) -> dict:
    """Helper: a valid kwargs bundle for `OracleVerdict.create`."""
    return {
        "sla_id": "sla-001",
        "artifact_hash": "a" * 64,
        "tier": 0,
        "result": "accepted",
        "evaluator_did": "did:companyos:oracle-node-001",
        "evidence": {"kind": "schema_pass", "detail": "all fields validated"},
        "issued_at": "2026-04-21T12:00:00Z",
        "keypair": keypair,
    }


# ---------------------------------------------------------------------------
# Construction + validation
# ---------------------------------------------------------------------------
class TestConstruction:
    def test_create_returns_fully_populated_instance(self, keypair):
        v = OracleVerdict.create(**_base_kwargs(keypair))
        assert v.sla_id == "sla-001"
        assert v.tier == 0
        assert v.result == "accepted"
        assert v.protocol_version == "companyos-verdict/0.1"
        assert v.score is None
        assert v.verdict_hash  # non-empty
        assert v.signer == keypair.public_key
        assert v.signature is not None

    def test_signer_matches_keypair_public_key(self, keypair):
        v = OracleVerdict.create(**_base_kwargs(keypair))
        assert v.signer == keypair.public_key
        assert v.signature.signer == keypair.public_key

    def test_empty_sla_id_rejected(self, keypair):
        kwargs = _base_kwargs(keypair)
        kwargs["sla_id"] = ""
        with pytest.raises(ValueError, match="sla_id"):
            OracleVerdict.create(**kwargs)

    def test_empty_artifact_hash_rejected(self, keypair):
        kwargs = _base_kwargs(keypair)
        kwargs["artifact_hash"] = ""
        with pytest.raises(ValueError, match="artifact_hash"):
            OracleVerdict.create(**kwargs)

    def test_invalid_tier_rejected(self, keypair):
        kwargs = _base_kwargs(keypair)
        kwargs["tier"] = 5
        with pytest.raises(ValueError, match="tier"):
            OracleVerdict.create(**kwargs)

    def test_invalid_result_rejected(self, keypair):
        kwargs = _base_kwargs(keypair)
        kwargs["result"] = "pending"
        with pytest.raises(ValueError, match="result"):
            OracleVerdict.create(**kwargs)

    def test_non_dict_evidence_rejected(self, keypair):
        kwargs = _base_kwargs(keypair)
        kwargs["evidence"] = "schema_pass"
        with pytest.raises(TypeError, match="evidence"):
            OracleVerdict.create(**kwargs)

    def test_unknown_evidence_kind_on_create_raises_verdict_error(self, keypair):
        kwargs = _base_kwargs(keypair)
        kwargs["evidence"] = {"kind": "totally_made_up"}
        with pytest.raises(VerdictError, match="unknown evidence kind"):
            OracleVerdict.create(**kwargs)

    def test_all_valid_tiers_accepted(self, keypair):
        for tier in (0, 1, 2, 3):
            kwargs = _base_kwargs(keypair)
            kwargs["tier"] = tier
            v = OracleVerdict.create(**kwargs)
            assert v.tier == tier

    def test_all_valid_results_accepted(self, keypair):
        for result in ("accepted", "rejected", "refunded"):
            kwargs = _base_kwargs(keypair)
            kwargs["result"] = result
            v = OracleVerdict.create(**kwargs)
            assert v.result == result

    def test_all_valid_evidence_kinds_accepted(self, keypair):
        from core.primitives.oracle import _VALID_EVIDENCE_KINDS
        for kind in _VALID_EVIDENCE_KINDS:
            kwargs = _base_kwargs(keypair)
            kwargs["evidence"] = {"kind": kind}
            v = OracleVerdict.create(**kwargs)
            assert v.evidence["kind"] == kind

    def test_score_decimal_accepted(self, keypair):
        kwargs = _base_kwargs(keypair)
        kwargs["score"] = Decimal("0.95")
        v = OracleVerdict.create(**kwargs)
        assert v.score == Decimal("0.95")

    def test_score_float_rejected(self, keypair):
        """score must be Decimal or None, not float."""
        kwargs = _base_kwargs(keypair)
        kwargs["score"] = 0.95
        with pytest.raises(TypeError, match="score"):
            OracleVerdict.create(**kwargs)

    def test_wrong_keypair_type_rejected(self, keypair):
        kwargs = _base_kwargs(keypair)
        kwargs["keypair"] = keypair.public_key  # pubkey, not keypair
        with pytest.raises(TypeError, match="keypair"):
            OracleVerdict.create(**kwargs)


# ---------------------------------------------------------------------------
# Canonical determinism
# ---------------------------------------------------------------------------
class TestCanonicalDeterminism:
    def test_dict_insertion_order_does_not_affect_verdict_hash(self, keypair):
        """Two `evidence` dicts with the same keys/values in different order
        must produce byte-identical canonical bytes and verdict_hash."""
        kwargs_a = _base_kwargs(keypair)
        kwargs_a["evidence"] = {"kind": "schema_pass", "detail": "ok", "count": 3}

        kwargs_b = _base_kwargs(keypair)
        kwargs_b["evidence"] = {"count": 3, "detail": "ok", "kind": "schema_pass"}

        v_a = OracleVerdict.create(**kwargs_a)
        v_b = OracleVerdict.create(**kwargs_b)

        assert v_a.verdict_hash == v_b.verdict_hash

    def test_nested_evidence_key_sorted(self, keypair):
        """Nested dicts inside evidence must be key-sorted so canonical bytes
        are deterministic regardless of insertion order."""
        kwargs_a = _base_kwargs(keypair)
        kwargs_a["evidence"] = {
            "kind": "schema_pass",
            "meta": {"z_field": 1, "a_field": 2},
        }
        kwargs_b = _base_kwargs(keypair)
        kwargs_b["evidence"] = {
            "kind": "schema_pass",
            "meta": {"a_field": 2, "z_field": 1},
        }

        v_a = OracleVerdict.create(**kwargs_a)
        v_b = OracleVerdict.create(**kwargs_b)
        assert v_a.verdict_hash == v_b.verdict_hash

    def test_verdict_hash_stable_across_independent_constructions(self, keypair):
        """Same inputs -> same verdict_hash every time (no randomness in hash)."""
        kwargs = _base_kwargs(keypair)
        hashes = {OracleVerdict.create(**kwargs).verdict_hash for _ in range(5)}
        assert len(hashes) == 1

    def test_different_sla_id_changes_verdict_hash(self, keypair):
        k1 = _base_kwargs(keypair)
        k2 = _base_kwargs(keypair)
        k2["sla_id"] = "sla-999"
        v1 = OracleVerdict.create(**k1)
        v2 = OracleVerdict.create(**k2)
        assert v1.verdict_hash != v2.verdict_hash

    def test_different_result_changes_verdict_hash(self, keypair):
        k1 = _base_kwargs(keypair)
        k2 = _base_kwargs(keypair)
        k2["result"] = "rejected"
        v1 = OracleVerdict.create(**k1)
        v2 = OracleVerdict.create(**k2)
        assert v1.verdict_hash != v2.verdict_hash

    def test_signature_excluded_from_canonical_bytes(self, keypair):
        """Canonical bytes computed from an OracleVerdict must not include
        the `signature` field, so the hash is independent of the signature."""
        v = OracleVerdict.create(**_base_kwargs(keypair))
        canon = _canonical_bytes(v, exclude_verdict_hash=False)
        # Decode and verify the key `signature` is absent.
        parsed = json.loads(canon.decode("utf-8"))
        assert "signature" not in parsed

    def test_verdict_hash_field_excluded_when_flag_set(self, keypair):
        """During hash computation, verdict_hash itself is excluded."""
        v = OracleVerdict.create(**_base_kwargs(keypair))
        body_no_hash = _canonical_bytes(v, exclude_verdict_hash=True)
        parsed = json.loads(body_no_hash.decode("utf-8"))
        assert "verdict_hash" not in parsed
        assert "signature" not in parsed

    def test_verdict_hash_field_included_in_signing_body(self, keypair):
        """Signing body includes verdict_hash (signature commits to the hash)."""
        v = OracleVerdict.create(**_base_kwargs(keypair))
        signing_body = _canonical_bytes(v, exclude_verdict_hash=False)
        parsed = json.loads(signing_body.decode("utf-8"))
        assert "verdict_hash" in parsed
        assert "signature" not in parsed


# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------
class TestSignatureVerification:
    def test_verify_signature_passes_on_fresh_verdict(self, keypair):
        v = OracleVerdict.create(**_base_kwargs(keypair))
        # Should not raise.
        v.verify_signature()

    def test_signature_round_trip_to_dict_from_dict(self, keypair):
        """create -> to_dict -> from_dict -> verify_signature must pass."""
        v = OracleVerdict.create(**_base_kwargs(keypair))
        rehydrated = OracleVerdict.from_dict(v.to_dict())
        # Should not raise.
        rehydrated.verify_signature()

    def test_tamper_evidence_after_signing_raises_signature_error(self, keypair):
        """Mutate evidence via dataclasses.replace -> verify_signature raises.

        This is the canonical tamper-detection test: changing any canonical
        field after the verdict_hash and signature were computed invalidates
        the signature because the signing body no longer matches.
        """
        v = OracleVerdict.create(**_base_kwargs(keypair))
        tampered = dataclasses.replace(
            v,
            evidence={"kind": "schema_fail", "detail": "injected"},
        )
        with pytest.raises(SignatureError):
            tampered.verify_signature()

    def test_tamper_result_after_signing_raises(self, keypair):
        v = OracleVerdict.create(**_base_kwargs(keypair))
        tampered = dataclasses.replace(v, result="rejected")
        with pytest.raises(SignatureError):
            tampered.verify_signature()

    def test_tamper_artifact_hash_after_signing_raises(self, keypair):
        v = OracleVerdict.create(**_base_kwargs(keypair))
        tampered = dataclasses.replace(v, artifact_hash="b" * 64)
        with pytest.raises(SignatureError):
            tampered.verify_signature()

    def test_signer_mismatch_raises_signature_error(self, keypair, other_keypair):
        """Replacing `signer` with a different public key (while leaving
        the signature intact) must raise SignatureError before the
        cryptographic check even runs."""
        v = OracleVerdict.create(**_base_kwargs(keypair))
        tampered = dataclasses.replace(v, signer=other_keypair.public_key)
        with pytest.raises(SignatureError, match="signer"):
            tampered.verify_signature()

    def test_two_different_verdicts_independent_signatures(
        self, keypair, other_keypair
    ):
        """Verdicts signed by different keypairs can each verify independently."""
        v1 = OracleVerdict.create(**_base_kwargs(keypair))
        kwargs2 = _base_kwargs(other_keypair)
        kwargs2["sla_id"] = "sla-002"
        v2 = OracleVerdict.create(**kwargs2)

        v1.verify_signature()
        v2.verify_signature()

    def test_different_keypairs_on_same_inputs_yield_different_signatures(
        self, keypair, other_keypair
    ):
        """Same payload, different keypair -> different signature bytes."""
        v1 = OracleVerdict.create(**_base_kwargs(keypair))
        kwargs2 = _base_kwargs(other_keypair)
        v2 = OracleVerdict.create(**kwargs2)

        # Hashes differ because signer is included in canonical bytes.
        assert v1.verdict_hash != v2.verdict_hash
        assert v1.signature.sig_hex != v2.signature.sig_hex


# ---------------------------------------------------------------------------
# Null score serialization
# ---------------------------------------------------------------------------
class TestNullScore:
    def test_null_score_serializes_as_null_not_absent(self, keypair):
        """score: None must appear as `"score": null` in to_dict, never absent."""
        v = OracleVerdict.create(**_base_kwargs(keypair))
        assert v.score is None
        d = v.to_dict()
        assert "score" in d
        assert d["score"] is None

    def test_null_score_present_in_json(self, keypair):
        """JSON encoding of to_dict must include `"score":null`."""
        v = OracleVerdict.create(**_base_kwargs(keypair))
        encoded = json.dumps(v.to_dict())
        assert '"score": null' in encoded or '"score":null' in encoded

    def test_non_null_score_serializes_as_fixed_notation_string(self, keypair):
        """score: Decimal("0.9500") -> "0.9500" (fixed notation, not scientific)."""
        kwargs = _base_kwargs(keypair)
        kwargs["score"] = Decimal("0.9500")
        v = OracleVerdict.create(**kwargs)
        d = v.to_dict()
        assert d["score"] == "0.9500"
        # No scientific notation.
        assert "e" not in d["score"].lower()

    def test_null_score_round_trips(self, keypair):
        """None score survives to_dict -> from_dict."""
        v = OracleVerdict.create(**_base_kwargs(keypair))
        rehydrated = OracleVerdict.from_dict(v.to_dict())
        assert rehydrated.score is None

    def test_decimal_score_round_trips(self, keypair):
        kwargs = _base_kwargs(keypair)
        kwargs["score"] = Decimal("0.75")
        v = OracleVerdict.create(**kwargs)
        rehydrated = OracleVerdict.from_dict(v.to_dict())
        assert rehydrated.score == Decimal("0.75")


# ---------------------------------------------------------------------------
# from_dict / to_dict round-trip
# ---------------------------------------------------------------------------
class TestRoundTrip:
    def test_full_round_trip_no_score(self, keypair):
        v = OracleVerdict.create(**_base_kwargs(keypair))
        rehydrated = OracleVerdict.from_dict(v.to_dict())
        assert rehydrated == v

    def test_full_round_trip_with_score(self, keypair):
        kwargs = _base_kwargs(keypair)
        kwargs["score"] = Decimal("0.88")
        v = OracleVerdict.create(**kwargs)
        rehydrated = OracleVerdict.from_dict(v.to_dict())
        assert rehydrated == v

    def test_from_dict_missing_required_field_raises(self, keypair):
        v = OracleVerdict.create(**_base_kwargs(keypair))
        for field in (
            "sla_id",
            "artifact_hash",
            "tier",
            "result",
            "evaluator_did",
            "evidence",
            "verdict_hash",
            "signer",
            "signature",
            "issued_at",
        ):
            d = v.to_dict()
            del d[field]
            with pytest.raises(ValueError, match=field):
                OracleVerdict.from_dict(d)

    def test_from_dict_unknown_evidence_kind_raises_verdict_error(self, keypair):
        v = OracleVerdict.create(**_base_kwargs(keypair))
        d = v.to_dict()
        d["evidence"] = {"kind": "completely_unknown_kind"}
        with pytest.raises(VerdictError, match="unknown evidence kind"):
            OracleVerdict.from_dict(d)

    def test_from_dict_none_evidence_kind_raises_verdict_error(self, keypair):
        """evidence dict without a 'kind' key (kind=None) raises VerdictError."""
        v = OracleVerdict.create(**_base_kwargs(keypair))
        d = v.to_dict()
        d["evidence"] = {"detail": "no kind field here"}
        with pytest.raises(VerdictError, match="unknown evidence kind"):
            OracleVerdict.from_dict(d)

    def test_to_dict_is_json_serializable(self, keypair):
        """to_dict output serializes to JSON without errors."""
        v = OracleVerdict.create(**_base_kwargs(keypair))
        encoded = json.dumps(v.to_dict(), sort_keys=True, separators=(",", ":"))
        decoded = json.loads(encoded)
        assert decoded["sla_id"] == "sla-001"
        assert decoded["protocol_version"] == "companyos-verdict/0.1"

    def test_from_dict_preserves_stored_verdict_hash(self, keypair):
        """from_dict preserves verdict_hash verbatim so callers can detect tamper
        (same contract as SLA from_dict preserving integrity_binding)."""
        v = OracleVerdict.create(**_base_kwargs(keypair))
        d = v.to_dict()
        original_hash = d["verdict_hash"]
        d["sla_id"] = "tampered-sla"
        rehydrated = OracleVerdict.from_dict(d)
        # Stored hash is preserved even though sla_id was changed.
        assert rehydrated.verdict_hash == original_hash

    def test_protocol_version_default_round_trips(self, keypair):
        v = OracleVerdict.create(**_base_kwargs(keypair))
        d = v.to_dict()
        del d["protocol_version"]
        rehydrated = OracleVerdict.from_dict(d)
        assert rehydrated.protocol_version == "companyos-verdict/0.1"

    def test_signer_round_trips_as_bytes_hex_dict(self, keypair):
        """to_dict serializes signer as {"bytes_hex": "..."}, not a string."""
        v = OracleVerdict.create(**_base_kwargs(keypair))
        d = v.to_dict()
        assert isinstance(d["signer"], dict)
        assert "bytes_hex" in d["signer"]
        assert d["signer"]["bytes_hex"] == keypair.public_key.bytes_hex


# ---------------------------------------------------------------------------
# Protocol version
# ---------------------------------------------------------------------------
class TestProtocolVersion:
    def test_default_protocol_version_populated(self, keypair):
        v = OracleVerdict.create(**_base_kwargs(keypair))
        assert v.protocol_version == "companyos-verdict/0.1"

    def test_custom_protocol_version_accepted(self, keypair):
        kwargs = _base_kwargs(keypair)
        kwargs["protocol_version"] = "companyos-verdict/0.2"
        v = OracleVerdict.create(**kwargs)
        assert v.protocol_version == "companyos-verdict/0.2"

    def test_protocol_version_participates_in_verdict_hash(self, keypair):
        """Changing protocol_version must change verdict_hash."""
        k1 = _base_kwargs(keypair)
        k2 = _base_kwargs(keypair)
        k2["protocol_version"] = "companyos-verdict/0.2"
        v1 = OracleVerdict.create(**k1)
        v2 = OracleVerdict.create(**k2)
        assert v1.verdict_hash != v2.verdict_hash
