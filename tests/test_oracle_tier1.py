"""
tests/test_oracle_tier1.py -- Ticket B2: Oracle.evaluate_tier1 coverage
=======================================================================
Tests for `Oracle.evaluate_tier1` and `Oracle.evaluate` dispatcher.

Coverage matrix:
- Happy path: schema passes, evaluator accepts with score >= accuracy_requirement
- Schema fail (mechanical gate): Tier 0 rejected verdict, evaluator NOT called
- Canonical hash mismatch: EvaluatorAuthorizationError
- Evaluator DID == requester DID: EvaluatorAuthorizationError
- Evaluator DID == provider DID: EvaluatorAuthorizationError
- Low score from evaluator: result="rejected" (evaluator decides, Oracle accepts it)
- Evaluator returns refunded with evaluator_error: verdict carries the evidence
- Tier 1 signature verifies
- Timeout: refunded verdict with evidence.kind="evaluator_timeout"
- Oracle.evaluate dispatcher: no primary_evaluator_did -> Tier 0
- Oracle.evaluate dispatcher: non-empty primary_evaluator_did -> Tier 1
- Mechanical fail with tier1_skipped_via_mechanical_fail flag set
"""
from __future__ import annotations

import hashlib
import json
import time
from decimal import Decimal

import pytest

from core.primitives.asset import AssetRef
from core.primitives.evaluator import EvaluationOutput
from core.primitives.exceptions import EvaluatorAuthorizationError
from core.primitives.identity import Ed25519Keypair
from core.primitives.money import Money
from core.primitives.oracle import Oracle
from core.primitives.schema_verifier import SchemaVerifier
from core.primitives.sla import InterOrgSLA
from tests.fixtures.evaluators import StubPassthroughEvaluator


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------
_EVALUATOR_DID = "did:companyos:evaluator-alpha"
_REQUESTER_DID = "did:companyos:requester-node"
_PROVIDER_DID = "did:companyos:provider-node"
_NODE_DID = "did:companyos:oracle-node-b2"
# 64-char lowercase hex strings required by B0-d coupling validation.
_CANONICAL_HASH = "abc123deadbeef" + "0" * 50  # 64 chars
_EVALUATOR_PUBKEY_HEX = "ee" * 32              # 64 chars


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
        node_did=_NODE_DID,
        node_keypair=node_keypair,
        schema_verifier=SchemaVerifier(),
        evaluator_timeout_sec=5,
    )


# ---------------------------------------------------------------------------
# SLA builders
# ---------------------------------------------------------------------------
def _make_money(usd: AssetRef) -> Money:
    return Money(quantity=Decimal("100.000000"), asset=usd)


def _valid_schema_envelope() -> dict:
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


def _make_tier1_sla(
    usd: AssetRef,
    artifact_bytes: bytes,
    *,
    schema_envelope: dict | None = None,
    primary_evaluator_did: str | None = _EVALUATOR_DID,
    canonical_evaluator_hash: str | None = _CANONICAL_HASH,
    primary_evaluator_pubkey_hex: str = _EVALUATOR_PUBKEY_HEX,
    requester_node_did: str = _REQUESTER_DID,
    provider_node_did: str = _PROVIDER_DID,
) -> InterOrgSLA:
    """Build a Tier 1 SLA with delivery hash bound.

    All three Tier 1 evaluator fields (primary_evaluator_did,
    canonical_evaluator_hash, primary_evaluator_pubkey_hex) must be
    provided together per the B0-d coupling rule, or all omitted.
    """
    if schema_envelope is None:
        schema_envelope = _valid_schema_envelope()
    sla = InterOrgSLA.create(
        sla_id="test-sla-tier1-001",
        requester_node_did=requester_node_did,
        provider_node_did=provider_node_did,
        task_scope="deliver analysis report (tier1)",
        deliverable_schema=schema_envelope,
        accuracy_requirement=0.8,
        latency_ms=60_000,
        payment=_make_money(usd),
        penalty_stake=_make_money(usd),
        nonce=InterOrgSLA.new_nonce(),
        issued_at="2026-04-21T00:00:00Z",
        expires_at="2026-04-28T00:00:00Z",
        primary_evaluator_did=primary_evaluator_did,
        canonical_evaluator_hash=canonical_evaluator_hash,
        primary_evaluator_pubkey_hex=primary_evaluator_pubkey_hex,
    )
    artifact_hash = hashlib.sha256(artifact_bytes).hexdigest()
    return sla.with_delivery_hash(artifact_hash)


def _stub_accepted(score: Decimal = Decimal("0.95")) -> StubPassthroughEvaluator:
    """Return a stub that reports an accepted evaluation."""
    output = EvaluationOutput(
        result="accepted",
        score=score,
        evidence={"kind": "schema_pass_with_score"},
        evaluator_canonical_hash=_CANONICAL_HASH,
    )
    return StubPassthroughEvaluator(
        evaluator_did=_EVALUATOR_DID,
        canonical_hash=_CANONICAL_HASH,
        canned_output=output,
    )


def _stub_rejected(score: Decimal = Decimal("0.5")) -> StubPassthroughEvaluator:
    """Return a stub that reports a rejected evaluation (low score)."""
    output = EvaluationOutput(
        result="rejected",
        score=score,
        evidence={"kind": "schema_pass_with_score"},
        evaluator_canonical_hash=_CANONICAL_HASH,
    )
    return StubPassthroughEvaluator(
        evaluator_did=_EVALUATOR_DID,
        canonical_hash=_CANONICAL_HASH,
        canned_output=output,
    )


def _stub_refunded_error() -> StubPassthroughEvaluator:
    """Return a stub that reports a refund due to evaluator_error."""
    output = EvaluationOutput(
        result="refunded",
        score=Decimal("0"),
        evidence={"kind": "evaluator_error", "detail": "internal evaluator crash"},
        evaluator_canonical_hash=_CANONICAL_HASH,
    )
    return StubPassthroughEvaluator(
        evaluator_did=_EVALUATOR_DID,
        canonical_hash=_CANONICAL_HASH,
        canned_output=output,
    )


# ---------------------------------------------------------------------------
# Helper: valid artifact
# ---------------------------------------------------------------------------
def _valid_artifact() -> bytes:
    return json.dumps({"summary": "all checks nominal", "score": 0.95}).encode()


def _invalid_artifact() -> bytes:
    """Missing required 'summary' field -> schema fail."""
    return json.dumps({"score": 0.75}).encode()


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------
class TestTier1HappyPath:
    def test_accepted_verdict_tier1(self, oracle: Oracle, usd: AssetRef):
        artifact = _valid_artifact()
        sla = _make_tier1_sla(usd, artifact)
        evaluator = _stub_accepted()

        verdict = oracle.evaluate_tier1(sla, artifact, evaluator=evaluator)

        assert verdict.result == "accepted"
        assert verdict.tier == 1
        assert verdict.score is not None
        assert verdict.evaluator_did == _EVALUATOR_DID

    def test_tier1_verdict_has_score(self, oracle: Oracle, usd: AssetRef):
        artifact = _valid_artifact()
        sla = _make_tier1_sla(usd, artifact)
        evaluator = _stub_accepted(score=Decimal("0.95"))

        verdict = oracle.evaluate_tier1(sla, artifact, evaluator=evaluator)

        assert verdict.score == Decimal("0.95")

    def test_tier1_verdict_protocol_version(self, oracle: Oracle, usd: AssetRef):
        artifact = _valid_artifact()
        sla = _make_tier1_sla(usd, artifact)
        evaluator = _stub_accepted()

        verdict = oracle.evaluate_tier1(sla, artifact, evaluator=evaluator)

        assert verdict.protocol_version == "companyos-verdict/0.2"

    def test_tier1_verdict_evidence_has_canonical_hash(
        self, oracle: Oracle, usd: AssetRef
    ):
        artifact = _valid_artifact()
        sla = _make_tier1_sla(usd, artifact)
        evaluator = _stub_accepted()

        verdict = oracle.evaluate_tier1(sla, artifact, evaluator=evaluator)

        assert "evaluator_canonical_hash" in verdict.evidence
        assert verdict.evidence["evaluator_canonical_hash"] == _CANONICAL_HASH

    def test_tier1_verdict_evidence_kind(self, oracle: Oracle, usd: AssetRef):
        artifact = _valid_artifact()
        sla = _make_tier1_sla(usd, artifact)
        evaluator = _stub_accepted()

        verdict = oracle.evaluate_tier1(sla, artifact, evaluator=evaluator)

        assert verdict.evidence["kind"] == "schema_pass_with_score"

    def test_tier1_sla_id_propagated(self, oracle: Oracle, usd: AssetRef):
        artifact = _valid_artifact()
        sla = _make_tier1_sla(usd, artifact)
        evaluator = _stub_accepted()

        verdict = oracle.evaluate_tier1(sla, artifact, evaluator=evaluator)

        assert verdict.sla_id == sla.sla_id

    def test_tier1_artifact_hash_correct(self, oracle: Oracle, usd: AssetRef):
        artifact = _valid_artifact()
        sla = _make_tier1_sla(usd, artifact)
        evaluator = _stub_accepted()

        verdict = oracle.evaluate_tier1(sla, artifact, evaluator=evaluator)

        assert verdict.artifact_hash == hashlib.sha256(artifact).hexdigest()


# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------
class TestTier1SignatureVerification:
    def test_tier1_verdict_signature_verifies(self, oracle: Oracle, usd: AssetRef):
        artifact = _valid_artifact()
        sla = _make_tier1_sla(usd, artifact)
        evaluator = _stub_accepted()

        verdict = oracle.evaluate_tier1(sla, artifact, evaluator=evaluator)

        # Must not raise.
        verdict.verify_signature()

    def test_tier1_rejected_verdict_signature_verifies(
        self, oracle: Oracle, usd: AssetRef
    ):
        artifact = _valid_artifact()
        sla = _make_tier1_sla(usd, artifact)
        evaluator = _stub_rejected()

        verdict = oracle.evaluate_tier1(sla, artifact, evaluator=evaluator)

        verdict.verify_signature()


# ---------------------------------------------------------------------------
# Mechanical gate (schema fail short-circuit)
# ---------------------------------------------------------------------------
class TestTier1MechanicalGate:
    def test_schema_fail_returns_tier0_verdict(self, oracle: Oracle, usd: AssetRef):
        artifact = _invalid_artifact()
        sla = _make_tier1_sla(usd, artifact)

        call_log: list[str] = []

        class SpyEvaluator(StubPassthroughEvaluator):
            def evaluate(self, sla, artifact_bytes, *, artifact_properties=None):
                call_log.append("called")
                return super().evaluate(sla, artifact_bytes, artifact_properties=artifact_properties)

        spy = SpyEvaluator(
            evaluator_did=_EVALUATOR_DID,
            canonical_hash=_CANONICAL_HASH,
            canned_output=EvaluationOutput(
                result="accepted",
                score=Decimal("0.9"),
                evidence={"kind": "schema_pass_with_score"},
                evaluator_canonical_hash=_CANONICAL_HASH,
            ),
        )

        verdict = oracle.evaluate_tier1(sla, artifact, evaluator=spy)

        # Evaluator must NOT have been called.
        assert call_log == [], "evaluator was called despite schema fail"
        assert verdict.tier == 0
        assert verdict.result == "rejected"

    def test_schema_fail_verdict_has_tier1_skipped_flag(
        self, oracle: Oracle, usd: AssetRef
    ):
        artifact = _invalid_artifact()
        sla = _make_tier1_sla(usd, artifact)
        evaluator = _stub_accepted()

        verdict = oracle.evaluate_tier1(sla, artifact, evaluator=evaluator)

        assert verdict.evidence.get("tier1_skipped_via_mechanical_fail") is True

    def test_schema_fail_missing_schema_returns_tier0_refund(
        self, oracle: Oracle, usd: AssetRef
    ):
        artifact = _valid_artifact()
        sla = _make_tier1_sla(usd, artifact, schema_envelope={})
        evaluator = _stub_accepted()

        verdict = oracle.evaluate_tier1(sla, artifact, evaluator=evaluator)

        assert verdict.tier == 0
        assert verdict.result == "refunded"
        assert verdict.evidence.get("tier1_skipped_via_mechanical_fail") is True


# ---------------------------------------------------------------------------
# Authorization checks
# ---------------------------------------------------------------------------
class TestTier1Authorization:
    def test_canonical_hash_mismatch_raises(self, oracle: Oracle, usd: AssetRef):
        # Use a 64-char hex string for the SLA field (B0-d coupling rule).
        _correct_hash = "1" * 64
        _wrong_hash = "2" * 64
        artifact = _valid_artifact()
        sla = _make_tier1_sla(
            usd, artifact, canonical_evaluator_hash=_correct_hash
        )

        wrong_hash_evaluator = StubPassthroughEvaluator(
            evaluator_did=_EVALUATOR_DID,
            canonical_hash=_wrong_hash,  # does NOT match SLA
            canned_output=EvaluationOutput(
                result="accepted",
                score=Decimal("0.9"),
                evidence={"kind": "schema_pass_with_score"},
                evaluator_canonical_hash=_wrong_hash,
            ),
        )

        with pytest.raises(EvaluatorAuthorizationError, match="canonical_hash"):
            oracle.evaluate_tier1(sla, artifact, evaluator=wrong_hash_evaluator)

    def test_no_canonical_hash_on_sla_skips_check(
        self, oracle: Oracle, usd: AssetRef
    ):
        """When sla.canonical_evaluator_hash is absent (Tier 0 SLA), the hash
        check is skipped and evaluate_tier1 runs without raising.

        B0-d coupling rule prevents setting primary_evaluator_did without
        canonical_evaluator_hash, so the "no hash" scenario is only
        constructible as a fully Tier 0 SLA (all evaluator fields absent).
        evaluate_tier1 does not itself enforce that the SLA be Tier 1.
        """
        artifact = _valid_artifact()
        artifact_hash = hashlib.sha256(artifact).hexdigest()
        # Tier 0 SLA: no evaluator fields set at all.
        sla = InterOrgSLA.create(
            sla_id="test-sla-no-hash-check",
            requester_node_did=_REQUESTER_DID,
            provider_node_did=_PROVIDER_DID,
            task_scope="no hash check test",
            deliverable_schema=_valid_schema_envelope(),
            accuracy_requirement=0.8,
            latency_ms=60_000,
            payment=_make_money(usd),
            penalty_stake=_make_money(usd),
            nonce=InterOrgSLA.new_nonce(),
            issued_at="2026-04-21T00:00:00Z",
            expires_at="2026-04-28T00:00:00Z",
            # primary_evaluator_did / canonical_evaluator_hash /
            # primary_evaluator_pubkey_hex all absent (Tier 0 defaults)
        ).with_delivery_hash(artifact_hash)
        assert sla.canonical_evaluator_hash is None

        # Evaluator has any hash string -- should not raise since the
        # oracle skips the hash check when canonical_evaluator_hash is falsy.
        evaluator = StubPassthroughEvaluator(
            evaluator_did=_EVALUATOR_DID,
            canonical_hash="any-hash-at-all",
            canned_output=EvaluationOutput(
                result="accepted",
                score=Decimal("0.9"),
                evidence={"kind": "schema_pass_with_score"},
                evaluator_canonical_hash="any-hash-at-all",
            ),
        )
        # Must not raise.
        verdict = oracle.evaluate_tier1(sla, artifact, evaluator=evaluator)
        assert verdict.tier == 1

    def test_evaluator_did_equals_requester_raises(
        self, oracle: Oracle, usd: AssetRef
    ):
        artifact = _valid_artifact()
        sla = _make_tier1_sla(usd, artifact)

        conflict_evaluator = StubPassthroughEvaluator(
            evaluator_did=_REQUESTER_DID,  # same as requester -- conflict
            canonical_hash=_CANONICAL_HASH,
            canned_output=EvaluationOutput(
                result="accepted",
                score=Decimal("0.9"),
                evidence={"kind": "schema_pass_with_score"},
                evaluator_canonical_hash=_CANONICAL_HASH,
            ),
        )

        with pytest.raises(EvaluatorAuthorizationError, match="requester_node_did"):
            oracle.evaluate_tier1(sla, artifact, evaluator=conflict_evaluator)

    def test_evaluator_did_equals_provider_raises(
        self, oracle: Oracle, usd: AssetRef
    ):
        artifact = _valid_artifact()
        sla = _make_tier1_sla(usd, artifact)

        conflict_evaluator = StubPassthroughEvaluator(
            evaluator_did=_PROVIDER_DID,  # same as provider -- conflict
            canonical_hash=_CANONICAL_HASH,
            canned_output=EvaluationOutput(
                result="accepted",
                score=Decimal("0.9"),
                evidence={"kind": "schema_pass_with_score"},
                evaluator_canonical_hash=_CANONICAL_HASH,
            ),
        )

        with pytest.raises(EvaluatorAuthorizationError, match="provider_node_did"):
            oracle.evaluate_tier1(sla, artifact, evaluator=conflict_evaluator)


# ---------------------------------------------------------------------------
# Evaluator outcome variants
# ---------------------------------------------------------------------------
class TestTier1EvaluatorOutcomes:
    def test_low_score_produces_rejected_verdict(
        self, oracle: Oracle, usd: AssetRef
    ):
        artifact = _valid_artifact()
        sla = _make_tier1_sla(usd, artifact)
        evaluator = _stub_rejected(score=Decimal("0.5"))

        verdict = oracle.evaluate_tier1(sla, artifact, evaluator=evaluator)

        assert verdict.result == "rejected"
        assert verdict.tier == 1
        assert verdict.score == Decimal("0.5")

    def test_evaluator_error_refunded_verdict_carries_evidence(
        self, oracle: Oracle, usd: AssetRef
    ):
        artifact = _valid_artifact()
        sla = _make_tier1_sla(usd, artifact)
        evaluator = _stub_refunded_error()

        verdict = oracle.evaluate_tier1(sla, artifact, evaluator=evaluator)

        assert verdict.result == "refunded"
        assert verdict.tier == 1
        assert verdict.evidence["kind"] == "evaluator_error"
        assert verdict.evidence.get("detail") == "internal evaluator crash"


# ---------------------------------------------------------------------------
# Timeout
# ---------------------------------------------------------------------------
class TestTier1Timeout:
    def test_timeout_returns_refunded_verdict(self, usd: AssetRef):
        node_keypair = Ed25519Keypair.generate()
        # Short timeout so the test stays fast.
        fast_timeout_oracle = Oracle(
            node_did=_NODE_DID,
            node_keypair=node_keypair,
            schema_verifier=SchemaVerifier(),
            evaluator_timeout_sec=1,
        )
        artifact = _valid_artifact()
        sla = _make_tier1_sla(usd, artifact)

        class SlowEvaluator(StubPassthroughEvaluator):
            def evaluate(self, sla, artifact_bytes, *, artifact_properties=None):
                time.sleep(10)  # well beyond the 1s timeout
                return super().evaluate(sla, artifact_bytes, artifact_properties=artifact_properties)

        slow_stub = SlowEvaluator(
            evaluator_did=_EVALUATOR_DID,
            canonical_hash=_CANONICAL_HASH,
            canned_output=EvaluationOutput(
                result="accepted",
                score=Decimal("0.9"),
                evidence={"kind": "schema_pass_with_score"},
                evaluator_canonical_hash=_CANONICAL_HASH,
            ),
        )

        verdict = fast_timeout_oracle.evaluate_tier1(sla, artifact, evaluator=slow_stub)

        assert verdict.result == "refunded"
        assert verdict.evidence["kind"] == "evaluator_timeout"
        assert "1s" in verdict.evidence.get("detail", "")
        assert verdict.score == Decimal("0")

    def test_timeout_verdict_signature_verifies(self, usd: AssetRef):
        node_keypair = Ed25519Keypair.generate()
        fast_timeout_oracle = Oracle(
            node_did=_NODE_DID,
            node_keypair=node_keypair,
            schema_verifier=SchemaVerifier(),
            evaluator_timeout_sec=1,
        )
        artifact = _valid_artifact()
        sla = _make_tier1_sla(usd, artifact)

        class SlowEvaluator(StubPassthroughEvaluator):
            def evaluate(self, sla, artifact_bytes, *, artifact_properties=None):
                time.sleep(10)
                return super().evaluate(sla, artifact_bytes, artifact_properties=artifact_properties)

        slow_stub = SlowEvaluator(
            evaluator_did=_EVALUATOR_DID,
            canonical_hash=_CANONICAL_HASH,
            canned_output=EvaluationOutput(
                result="accepted",
                score=Decimal("0.9"),
                evidence={"kind": "schema_pass_with_score"},
                evaluator_canonical_hash=_CANONICAL_HASH,
            ),
        )

        verdict = fast_timeout_oracle.evaluate_tier1(sla, artifact, evaluator=slow_stub)

        # Must not raise.
        verdict.verify_signature()


# ---------------------------------------------------------------------------
# Oracle.evaluate dispatcher
# ---------------------------------------------------------------------------
class TestOracleEvaluateDispatcher:
    def test_no_primary_evaluator_did_routes_to_tier0(
        self, oracle: Oracle, usd: AssetRef
    ):
        """SLA without primary_evaluator_did -> evaluate_tier0 path."""
        artifact = _valid_artifact()
        # Build a Tier 0 SLA (no primary_evaluator_did).
        sla = InterOrgSLA.create(
            sla_id="test-sla-dispatch-tier0",
            requester_node_did=_REQUESTER_DID,
            provider_node_did=_PROVIDER_DID,
            task_scope="tier0 dispatch test",
            deliverable_schema=_valid_schema_envelope(),
            accuracy_requirement=0.8,
            latency_ms=60_000,
            payment=Money(quantity=Decimal("100.000000"), asset=usd),
            penalty_stake=Money(quantity=Decimal("100.000000"), asset=usd),
            nonce=InterOrgSLA.new_nonce(),
            issued_at="2026-04-21T00:00:00Z",
            expires_at="2026-04-28T00:00:00Z",
            # primary_evaluator_did intentionally omitted
        )
        artifact_hash = hashlib.sha256(artifact).hexdigest()
        sla = sla.with_delivery_hash(artifact_hash)

        verdict = oracle.evaluate(sla, artifact, evaluator=None)

        assert verdict.tier == 0

    def test_primary_evaluator_did_routes_to_tier1(
        self, oracle: Oracle, usd: AssetRef
    ):
        """SLA with primary_evaluator_did -> evaluate_tier1 path."""
        artifact = _valid_artifact()
        sla = _make_tier1_sla(usd, artifact)
        evaluator = _stub_accepted()

        verdict = oracle.evaluate(sla, artifact, evaluator=evaluator)

        assert verdict.tier == 1

    def test_tier1_route_requires_evaluator(self, oracle: Oracle, usd: AssetRef):
        """Missing evaluator when Tier 1 is selected raises ValueError."""
        artifact = _valid_artifact()
        sla = _make_tier1_sla(usd, artifact)

        with pytest.raises(ValueError, match="evaluator is required"):
            oracle.evaluate(sla, artifact, evaluator=None)
