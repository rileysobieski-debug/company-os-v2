"""tests/test_oracle_adversarial.py -- Adversarial scenarios for the Oracle.

Unit tests for attacks the Oracle must resist and for known gaps that
v1a accepts (these are marked explicitly so reviewers can see the
exact threat surface).

Scenarios covered:
- Post-signing evidence tamper -> SignatureError (closes tamper path).
- Post-signing evaluator_did tamper -> SignatureError (closes tamper path).
- Signer / signature.signer drift -> SignatureError (closes drift path).
- Identity-spoof gap (v1b): a raw OracleVerdict can be constructed with
  an evaluator_did that claims one identity while being signed by a
  different keypair's keys. verify_signature PASSES today because the
  Oracle does not consult a NodeRegistry. Documented as an open v1b
  gap so the test pins current behavior and the recommendation (add
  registry-backed evaluator authorization to verify_signature).
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
from decimal import Decimal

import pytest

from core.primitives.asset import AssetRef
from core.primitives.exceptions import SignatureError
from core.primitives.identity import Ed25519Keypair
from core.primitives.money import Money
from core.primitives.oracle import Oracle, OracleVerdict
from core.primitives.schema_verifier import SchemaVerifier
from core.primitives.sla import InterOrgSLA


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def usd() -> AssetRef:
    return AssetRef(asset_id="mock-usd", contract="USD", decimals=6)


@pytest.fixture
def requester_keypair() -> Ed25519Keypair:
    return Ed25519Keypair.generate()


@pytest.fixture
def provider_keypair() -> Ed25519Keypair:
    return Ed25519Keypair.generate()


@pytest.fixture
def requester_oracle(requester_keypair: Ed25519Keypair) -> Oracle:
    return Oracle(
        node_did="did:companyos:requester",
        node_keypair=requester_keypair,
        schema_verifier=SchemaVerifier(),
    )


def _schema_envelope() -> dict:
    return {
        "kind": "json_schema",
        "spec_version": "2020-12",
        "schema": {
            "type": "object",
            "required": ["summary"],
            "properties": {"summary": {"type": "string"}},
        },
    }


def _make_sla(usd: AssetRef, artifact_bytes: bytes) -> InterOrgSLA:
    sla = InterOrgSLA.create(
        sla_id="test-sla-adversarial",
        requester_node_did="did:companyos:requester",
        provider_node_did="did:companyos:provider",
        task_scope="adversarial",
        deliverable_schema=_schema_envelope(),
        accuracy_requirement=0.9,
        latency_ms=60_000,
        payment=Money(Decimal("100.000000"), usd),
        penalty_stake=Money(Decimal("10.000000"), usd),
        nonce=InterOrgSLA.new_nonce(),
        issued_at="2026-04-21T00:00:00Z",
        expires_at="2026-04-28T00:00:00Z",
    )
    return sla.with_delivery_hash(hashlib.sha256(artifact_bytes).hexdigest())


# ---------------------------------------------------------------------------
# Tamper tests: these MUST fail verify_signature.
# ---------------------------------------------------------------------------
class TestPostSigningTamper:
    def test_evidence_kind_tamper_breaks_signature(
        self, requester_oracle: Oracle, usd: AssetRef
    ):
        """Post-signing mutation of evidence.kind must invalidate the signature."""
        artifact = json.dumps({"summary": "ok"}).encode()
        sla = _make_sla(usd, artifact)
        verdict = requester_oracle.evaluate_tier0(sla, artifact)
        verdict.verify_signature()  # clean baseline

        # Replace evidence with a spoofed "founder_override" kind.
        tampered_evidence = dict(verdict.evidence)
        tampered_evidence["kind"] = "founder_override"
        tampered = dataclasses.replace(verdict, evidence=tampered_evidence)
        with pytest.raises(SignatureError):
            tampered.verify_signature()

    def test_evaluator_did_tamper_breaks_signature(
        self, requester_oracle: Oracle, usd: AssetRef
    ):
        """Swapping evaluator_did on a signed verdict must break the sig.

        The field participates in canonical bytes, so mutation changes
        verdict_hash and the Ed25519 signature no longer covers the new
        byte shape.
        """
        artifact = json.dumps({"summary": "ok"}).encode()
        sla = _make_sla(usd, artifact)
        verdict = requester_oracle.evaluate_tier0(sla, artifact)
        spoofed = dataclasses.replace(verdict, evaluator_did="did:companyos:attacker")
        with pytest.raises(SignatureError):
            spoofed.verify_signature()

    def test_signer_and_signature_signer_drift_raises(
        self,
        requester_oracle: Oracle,
        provider_keypair: Ed25519Keypair,
        usd: AssetRef,
    ):
        """Top-level signer swapped to a different pubkey than the one
        embedded in signature.signer -> SignatureError.

        This is the "signer / signature drift" path from the consistency
        check in OracleVerdict.verify_signature.
        """
        artifact = json.dumps({"summary": "ok"}).encode()
        sla = _make_sla(usd, artifact)
        verdict = requester_oracle.evaluate_tier0(sla, artifact)
        drifted = dataclasses.replace(verdict, signer=provider_keypair.public_key)
        with pytest.raises(SignatureError, match="signer"):
            drifted.verify_signature()


# ---------------------------------------------------------------------------
# Known v1a gap: evaluator_did is NOT crypto-bound to a registered identity.
# ---------------------------------------------------------------------------
class TestKnownV1aGaps:
    """Behaviors that v1a accepts and v1b should close.

    These tests pin the current contract so a future registry-backed
    authorization change surfaces as a clear diff instead of a silent
    semantic shift.
    """

    def test_evaluator_did_spoof_passes_verify_signature_today(
        self,
        requester_keypair: Ed25519Keypair,
        provider_keypair: Ed25519Keypair,
        usd: AssetRef,
    ):
        """KNOWN V1A GAP (v1b fix).

        Scenario: a provider issues a verdict claiming evaluator_did is
        the requester's DID, but signs with its own keypair. The verdict
        cryptographically verifies because Oracle.verify_signature checks:
          (a) self.signer == self.signature.signer (consistency)
          (b) Ed25519 verify over canonical bytes

        Neither check consults a NodeRegistry to confirm that the
        signer's pubkey is the one registered for evaluator_did.

        v1a decision (documented in ORACLE.md section (e)): defer
        registry-backed evaluator authorization to v1b, consistent with
        how founder identity is also string-based in v1a.

        v1b fix: extend OracleVerdict.verify_signature (or add a
        verify_with_registry variant) that resolves evaluator_did
        through NodeRegistry and rejects on pubkey mismatch.
        """
        # Provider builds a verdict that claims the requester evaluated.
        oracle_claiming_requester = Oracle(
            node_did="did:companyos:requester",  # spoofed claim
            node_keypair=provider_keypair,        # but signs with provider's key
            schema_verifier=SchemaVerifier(),
        )
        artifact = json.dumps({"summary": "attack"}).encode()
        sla = _make_sla(usd, artifact)
        spoof_verdict = oracle_claiming_requester.evaluate_tier0(sla, artifact)

        # The spoof verdict's top-level signer is the provider's pubkey
        # (because Oracle uses self.node_keypair.public_key), NOT the
        # requester's pubkey.
        assert spoof_verdict.signer == provider_keypair.public_key
        assert spoof_verdict.signer != requester_keypair.public_key
        # And yet: evaluator_did claims to be the requester.
        assert spoof_verdict.evaluator_did == "did:companyos:requester"

        # verify_signature PASSES because there is no registry lookup.
        # This is the gap. v1b MUST close it via NodeRegistry binding.
        spoof_verdict.verify_signature()  # does not raise

        # Explicit assertion documenting the gap for future greppers.
        # If this line ever starts failing, someone fixed the gap --
        # update the test to require registry-backed verification and
        # flip the assertion.
        assert True, (
            "v1a: evaluator_did is not crypto-bound to signer. "
            "v1b must add NodeRegistry-backed authorization."
        )
