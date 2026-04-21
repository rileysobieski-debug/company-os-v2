"""
tests/test_oracle_tier0.py -- Ticket A4 Tier 0 coverage for Oracle
===================================================================
Unit tests for `Oracle.evaluate_tier0`:

- Happy path: SLA with valid schema + matching artifact -> accepted verdict.
- Schema fail: artifact missing a required field -> rejected verdict.
- Missing schema: SLA with no deliverable_schema -> refunded verdict.
- Evaluator DID: verdict.evaluator_did equals the Oracle node_did.
- Signature verification: each verdict passes verify_signature().
- Artifact hash: verdict.artifact_hash equals sha256(artifact_bytes).
- Tier: all Tier 0 verdicts carry tier=0.
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal

import pytest

from core.primitives.asset import AssetRef
from core.primitives.identity import Ed25519Keypair
from core.primitives.money import Money
from core.primitives.oracle import Oracle
from core.primitives.schema_verifier import SchemaVerifier
from core.primitives.sla import InterOrgSLA


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def usd() -> AssetRef:
    return AssetRef(asset_id="mock-usd", contract="USD", decimals=6)


@pytest.fixture
def node_keypair() -> Ed25519Keypair:
    return Ed25519Keypair.generate()


@pytest.fixture
def oracle(node_keypair: Ed25519Keypair) -> Oracle:
    return Oracle(
        node_did="did:companyos:oracle-node-a",
        node_keypair=node_keypair,
        schema_verifier=SchemaVerifier(),
    )


# ---------------------------------------------------------------------------
# SLA builders
# ---------------------------------------------------------------------------
def _make_money(usd: AssetRef) -> Money:
    return Money(quantity=Decimal("100.000000"), asset=usd)


def _valid_schema_envelope() -> dict:
    """A well-formed json_schema envelope requiring a 'summary' field."""
    return {
        "kind": "json_schema",
        "spec_version": "2020-12",
        "schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "score": {"type": "number"},
            },
            "required": ["summary"],
        },
    }


def _make_sla(usd: AssetRef, schema_envelope: dict | None = None) -> InterOrgSLA:
    """Create a base SLA, optionally with a specific schema envelope.

    Does NOT bind an artifact hash - callers must call with_delivery_hash.
    """
    if schema_envelope is None:
        schema_envelope = _valid_schema_envelope()
    return InterOrgSLA.create(
        sla_id="test-sla-tier0-001",
        requester_node_did="did:companyos:requester",
        provider_node_did="did:companyos:provider",
        task_scope="deliver analysis report",
        deliverable_schema=schema_envelope,
        accuracy_requirement=0.9,
        latency_ms=60_000,
        payment=_make_money(usd),
        penalty_stake=_make_money(usd),
        nonce=InterOrgSLA.new_nonce(),
        issued_at="2026-04-21T00:00:00Z",
        expires_at="2026-04-28T00:00:00Z",
    )


def _make_sla_with_hash(
    usd: AssetRef,
    artifact_bytes: bytes,
    schema_envelope: dict | None = None,
) -> InterOrgSLA:
    """Create a hash-bound SLA ready for SchemaVerifier / Oracle evaluation."""
    sla = _make_sla(usd, schema_envelope)
    artifact_hash = hashlib.sha256(artifact_bytes).hexdigest()
    return sla.with_delivery_hash(artifact_hash)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------
class TestTier0HappyPath:
    def test_accepted_verdict_on_valid_artifact(self, oracle: Oracle, usd: AssetRef):
        artifact = json.dumps({"summary": "All systems nominal", "score": 0.95}).encode()
        sla = _make_sla_with_hash(usd, artifact)
        verdict = oracle.evaluate_tier0(sla, artifact)

        assert verdict.result == "accepted"
        assert verdict.tier == 0
        assert verdict.evidence["kind"] == "schema_pass"

    def test_signature_verifies_on_accepted_verdict(self, oracle: Oracle, usd: AssetRef):
        artifact = json.dumps({"summary": "ok"}).encode()
        sla = _make_sla_with_hash(usd, artifact)
        verdict = oracle.evaluate_tier0(sla, artifact)
        # Must not raise.
        verdict.verify_signature()

    def test_evaluator_did_matches_node_did(self, oracle: Oracle, usd: AssetRef):
        artifact = json.dumps({"summary": "report text"}).encode()
        sla = _make_sla_with_hash(usd, artifact)
        verdict = oracle.evaluate_tier0(sla, artifact)
        assert verdict.evaluator_did == oracle.node_did

    def test_artifact_hash_matches_sha256(self, oracle: Oracle, usd: AssetRef):
        artifact = json.dumps({"summary": "hash test"}).encode()
        sla = _make_sla_with_hash(usd, artifact)
        verdict = oracle.evaluate_tier0(sla, artifact)
        expected_hash = hashlib.sha256(artifact).hexdigest()
        assert verdict.artifact_hash == expected_hash

    def test_tier_is_zero(self, oracle: Oracle, usd: AssetRef):
        artifact = json.dumps({"summary": "tier check"}).encode()
        sla = _make_sla_with_hash(usd, artifact)
        verdict = oracle.evaluate_tier0(sla, artifact)
        assert verdict.tier == 0

    def test_sla_id_propagated(self, oracle: Oracle, usd: AssetRef):
        artifact = json.dumps({"summary": "sla propagation"}).encode()
        sla = _make_sla_with_hash(usd, artifact)
        verdict = oracle.evaluate_tier0(sla, artifact)
        assert verdict.sla_id == sla.sla_id


# ---------------------------------------------------------------------------
# Schema fail (rejected)
# ---------------------------------------------------------------------------
class TestTier0SchemaFail:
    def test_rejected_on_missing_required_field(self, oracle: Oracle, usd: AssetRef):
        # Artifact is valid JSON but missing the required 'summary' field.
        artifact = json.dumps({"score": 0.75}).encode()
        sla = _make_sla_with_hash(usd, artifact)
        verdict = oracle.evaluate_tier0(sla, artifact)

        assert verdict.result == "rejected"
        assert verdict.evidence["kind"] == "schema_fail"

    def test_signature_verifies_on_rejected_verdict(self, oracle: Oracle, usd: AssetRef):
        artifact = json.dumps({"score": 0.75}).encode()
        sla = _make_sla_with_hash(usd, artifact)
        verdict = oracle.evaluate_tier0(sla, artifact)
        # Must not raise even for a rejected verdict.
        verdict.verify_signature()

    def test_evaluator_did_present_on_rejected_verdict(self, oracle: Oracle, usd: AssetRef):
        artifact = json.dumps({"score": 0.75}).encode()
        sla = _make_sla_with_hash(usd, artifact)
        verdict = oracle.evaluate_tier0(sla, artifact)
        assert verdict.evaluator_did == oracle.node_did

    def test_artifact_hash_correct_on_rejected_verdict(self, oracle: Oracle, usd: AssetRef):
        artifact = json.dumps({"score": 0.75}).encode()
        sla = _make_sla_with_hash(usd, artifact)
        verdict = oracle.evaluate_tier0(sla, artifact)
        assert verdict.artifact_hash == hashlib.sha256(artifact).hexdigest()


# ---------------------------------------------------------------------------
# Missing schema (refunded)
# ---------------------------------------------------------------------------
class TestTier0MissingSchema:
    def test_refunded_on_sla_with_no_deliverable_schema(
        self, oracle: Oracle, usd: AssetRef
    ):
        # Build a hash-bound SLA with an empty schema envelope so that
        # SchemaVerifier returns refunded/sla_missing_schema at step 2.
        artifact = json.dumps({"summary": "irrelevant"}).encode()
        sla = _make_sla_with_hash(usd, artifact, schema_envelope={})
        verdict = oracle.evaluate_tier0(sla, artifact)

        assert verdict.result == "refunded"
        assert verdict.evidence["kind"] == "sla_missing_schema"

    def test_signature_verifies_on_refunded_verdict(self, oracle: Oracle, usd: AssetRef):
        artifact = json.dumps({"summary": "irrelevant"}).encode()
        sla = _make_sla_with_hash(usd, artifact, schema_envelope={})
        verdict = oracle.evaluate_tier0(sla, artifact)
        # Must not raise even for a refunded verdict.
        verdict.verify_signature()

    def test_evaluator_did_present_on_refunded_verdict(self, oracle: Oracle, usd: AssetRef):
        artifact = json.dumps({"summary": "irrelevant"}).encode()
        sla = _make_sla_with_hash(usd, artifact, schema_envelope={})
        verdict = oracle.evaluate_tier0(sla, artifact)
        assert verdict.evaluator_did == oracle.node_did

    def test_artifact_hash_correct_on_refunded_verdict(self, oracle: Oracle, usd: AssetRef):
        artifact = json.dumps({"summary": "irrelevant"}).encode()
        sla = _make_sla_with_hash(usd, artifact, schema_envelope={})
        verdict = oracle.evaluate_tier0(sla, artifact)
        assert verdict.artifact_hash == hashlib.sha256(artifact).hexdigest()


# ---------------------------------------------------------------------------
# Oracle constructor validation
# ---------------------------------------------------------------------------
class TestOracleConstructor:
    def test_empty_node_did_raises(self):
        kp = Ed25519Keypair.generate()
        with pytest.raises(ValueError, match="node_did"):
            Oracle(node_did="", node_keypair=kp, schema_verifier=SchemaVerifier())

    def test_whitespace_node_did_raises(self):
        kp = Ed25519Keypair.generate()
        with pytest.raises(ValueError, match="node_did"):
            Oracle(node_did="   ", node_keypair=kp, schema_verifier=SchemaVerifier())

    def test_non_keypair_raises(self):
        kp = Ed25519Keypair.generate()
        with pytest.raises(TypeError, match="node_keypair"):
            Oracle(
                node_did="did:companyos:x",
                node_keypair=kp.public_key,  # type: ignore
                schema_verifier=SchemaVerifier(),
            )
