"""
tests/test_oracle_tier3.py -- Ticket A4 Tier 3 coverage for Oracle
===================================================================
Unit tests for `Oracle.founder_override`:

- Happy path: founder overrides a rejected verdict with result="accepted".
- evidence.kind = "founder_override".
- evidence.overrides = prior_verdict.verdict_hash.
- evidence.reason string is present and matches the supplied reason.
- evidence.founder_identity is present.
- Tier 3 verdict preserves sla_id and artifact_hash from the prior verdict.
- Signature verifies with the founder's keypair (not the node keypair).
- Non-founder identity raises SignatureError.
- Empty or whitespace reason raises ValueError.
- Third-party replay: Oracle B (different node) can verify Oracle A's verdict
  without needing A's private key.
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal

import pytest

from core.primitives.asset import AssetRef
from core.primitives.exceptions import SignatureError
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
def node_a_keypair() -> Ed25519Keypair:
    return Ed25519Keypair.generate()


@pytest.fixture
def node_b_keypair() -> Ed25519Keypair:
    return Ed25519Keypair.generate()


@pytest.fixture
def founder_keypair() -> Ed25519Keypair:
    return Ed25519Keypair.generate()


@pytest.fixture
def oracle_a(node_a_keypair: Ed25519Keypair) -> Oracle:
    return Oracle(
        node_did="did:companyos:oracle-node-a",
        node_keypair=node_a_keypair,
        schema_verifier=SchemaVerifier(),
    )


@pytest.fixture
def oracle_b(node_b_keypair: Ed25519Keypair) -> Oracle:
    return Oracle(
        node_did="did:companyos:oracle-node-b",
        node_keypair=node_b_keypair,
        schema_verifier=SchemaVerifier(),
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
            },
            "required": ["summary"],
        },
    }


def _make_sla_with_hash(usd: AssetRef, artifact_bytes: bytes) -> InterOrgSLA:
    sla = InterOrgSLA.create(
        sla_id="test-sla-tier3-001",
        requester_node_did="did:companyos:requester",
        provider_node_did="did:companyos:provider",
        task_scope="deliver report for founder override test",
        deliverable_schema=_valid_schema_envelope(),
        accuracy_requirement=0.9,
        latency_ms=60_000,
        payment=_make_money(usd),
        penalty_stake=_make_money(usd),
        nonce=InterOrgSLA.new_nonce(),
        issued_at="2026-04-21T00:00:00Z",
        expires_at="2026-04-28T00:00:00Z",
    )
    return sla.with_delivery_hash(hashlib.sha256(artifact_bytes).hexdigest())


def _make_rejected_verdict(
    oracle: Oracle, usd: AssetRef
) -> tuple["OracleVerdict", bytes, "InterOrgSLA"]:
    """Create a rejected Tier 0 verdict (artifact missing required field)."""
    from core.primitives.oracle import OracleVerdict  # noqa: F401 - type hint only
    artifact = json.dumps({"score": 0.5}).encode()  # missing 'summary'
    sla = _make_sla_with_hash(usd, artifact)
    verdict = oracle.evaluate_tier0(sla, artifact)
    assert verdict.result == "rejected"
    return verdict, artifact, sla


# ---------------------------------------------------------------------------
# Founder override happy path
# ---------------------------------------------------------------------------
class TestFounderOverrideHappyPath:
    def test_tier3_result_accepted(
        self, oracle_a: Oracle, usd: AssetRef, founder_keypair: Ed25519Keypair
    ):
        prior, _, _ = _make_rejected_verdict(oracle_a, usd)
        override = oracle_a.founder_override(
            prior,
            result="accepted",
            reason="Reviewed manually; artifact meets intent.",
            founder_keypair=founder_keypair,
            founder_identity="riley",
        )
        assert override.result == "accepted"
        assert override.tier == 3

    def test_evidence_kind_is_founder_override(
        self, oracle_a: Oracle, usd: AssetRef, founder_keypair: Ed25519Keypair
    ):
        prior, _, _ = _make_rejected_verdict(oracle_a, usd)
        override = oracle_a.founder_override(
            prior,
            result="accepted",
            reason="Manual review passed.",
            founder_keypair=founder_keypair,
            founder_identity="riley",
        )
        assert override.evidence["kind"] == "founder_override"

    def test_evidence_overrides_equals_prior_verdict_hash(
        self, oracle_a: Oracle, usd: AssetRef, founder_keypair: Ed25519Keypair
    ):
        prior, _, _ = _make_rejected_verdict(oracle_a, usd)
        override = oracle_a.founder_override(
            prior,
            result="accepted",
            reason="Artifact acceptable on review.",
            founder_keypair=founder_keypair,
            founder_identity="riley",
        )
        assert override.evidence["overrides"] == prior.verdict_hash

    def test_evidence_reason_string_present(
        self, oracle_a: Oracle, usd: AssetRef, founder_keypair: Ed25519Keypair
    ):
        reason = "Schema too strict; intent is met."
        prior, _, _ = _make_rejected_verdict(oracle_a, usd)
        override = oracle_a.founder_override(
            prior,
            result="accepted",
            reason=reason,
            founder_keypair=founder_keypair,
            founder_identity="riley",
        )
        assert override.evidence["reason"] == reason

    def test_evidence_founder_identity_present(
        self, oracle_a: Oracle, usd: AssetRef, founder_keypair: Ed25519Keypair
    ):
        prior, _, _ = _make_rejected_verdict(oracle_a, usd)
        override = oracle_a.founder_override(
            prior,
            result="accepted",
            reason="Accepted on founder review.",
            founder_keypair=founder_keypair,
            founder_identity="riley",
        )
        assert override.evidence["founder_identity"] == "riley"

    def test_sla_id_preserved_from_prior_verdict(
        self, oracle_a: Oracle, usd: AssetRef, founder_keypair: Ed25519Keypair
    ):
        prior, _, _ = _make_rejected_verdict(oracle_a, usd)
        override = oracle_a.founder_override(
            prior,
            result="accepted",
            reason="Override reason.",
            founder_keypair=founder_keypair,
            founder_identity="riley",
        )
        assert override.sla_id == prior.sla_id

    def test_artifact_hash_preserved_from_prior_verdict(
        self, oracle_a: Oracle, usd: AssetRef, founder_keypair: Ed25519Keypair
    ):
        prior, _, _ = _make_rejected_verdict(oracle_a, usd)
        override = oracle_a.founder_override(
            prior,
            result="accepted",
            reason="Override reason.",
            founder_keypair=founder_keypair,
            founder_identity="riley",
        )
        assert override.artifact_hash == prior.artifact_hash

    def test_signature_verifies_with_founder_keypair(
        self, oracle_a: Oracle, usd: AssetRef, founder_keypair: Ed25519Keypair
    ):
        prior, _, _ = _make_rejected_verdict(oracle_a, usd)
        override = oracle_a.founder_override(
            prior,
            result="accepted",
            reason="Founder review passed.",
            founder_keypair=founder_keypair,
            founder_identity="riley",
        )
        # Must not raise - the embedded signer is the founder's pubkey.
        override.verify_signature()
        # Verify the signer field is the founder's public key.
        assert override.signer == founder_keypair.public_key

    def test_evaluator_did_is_node_did(
        self, oracle_a: Oracle, usd: AssetRef, founder_keypair: Ed25519Keypair
    ):
        """evaluator_did records the node, not the founder."""
        prior, _, _ = _make_rejected_verdict(oracle_a, usd)
        override = oracle_a.founder_override(
            prior,
            result="accepted",
            reason="Override reason.",
            founder_keypair=founder_keypair,
            founder_identity="riley",
        )
        assert override.evaluator_did == oracle_a.node_did

    def test_all_founder_principals_accepted(
        self, oracle_a: Oracle, usd: AssetRef, founder_keypair: Ed25519Keypair
    ):
        """All three FOUNDER_PRINCIPALS strings should be accepted."""
        from core.primitives.state import FOUNDER_PRINCIPALS
        for identity in FOUNDER_PRINCIPALS:
            prior, _, _ = _make_rejected_verdict(oracle_a, usd)
            override = oracle_a.founder_override(
                prior,
                result="accepted",
                reason=f"Override by {identity}.",
                founder_keypair=founder_keypair,
                founder_identity=identity,
            )
            assert override.evidence["founder_identity"] == identity
            override.verify_signature()


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------
class TestFounderOverrideAccessControl:
    def test_non_founder_identity_raises_signature_error(
        self, oracle_a: Oracle, usd: AssetRef, founder_keypair: Ed25519Keypair
    ):
        prior, _, _ = _make_rejected_verdict(oracle_a, usd)
        with pytest.raises(SignatureError, match="non-founder identity"):
            oracle_a.founder_override(
                prior,
                result="accepted",
                reason="Attempting unauthorized override.",
                founder_keypair=founder_keypair,
                founder_identity="mallory",
            )

    def test_empty_founder_identity_raises_signature_error(
        self, oracle_a: Oracle, usd: AssetRef, founder_keypair: Ed25519Keypair
    ):
        prior, _, _ = _make_rejected_verdict(oracle_a, usd)
        with pytest.raises(SignatureError):
            oracle_a.founder_override(
                prior,
                result="accepted",
                reason="Override attempt.",
                founder_keypair=founder_keypair,
                founder_identity="",
            )

    def test_empty_reason_raises_value_error(
        self, oracle_a: Oracle, usd: AssetRef, founder_keypair: Ed25519Keypair
    ):
        prior, _, _ = _make_rejected_verdict(oracle_a, usd)
        with pytest.raises(ValueError, match="reason"):
            oracle_a.founder_override(
                prior,
                result="accepted",
                reason="",
                founder_keypair=founder_keypair,
                founder_identity="riley",
            )

    def test_whitespace_reason_raises_value_error(
        self, oracle_a: Oracle, usd: AssetRef, founder_keypair: Ed25519Keypair
    ):
        prior, _, _ = _make_rejected_verdict(oracle_a, usd)
        with pytest.raises(ValueError, match="reason"):
            oracle_a.founder_override(
                prior,
                result="accepted",
                reason="   ",
                founder_keypair=founder_keypair,
                founder_identity="riley",
            )


# ---------------------------------------------------------------------------
# Third-party replay
# ---------------------------------------------------------------------------
class TestThirdPartyReplay:
    def test_oracle_b_can_verify_oracle_a_tier0_verdict(
        self,
        oracle_a: Oracle,
        oracle_b: Oracle,
        usd: AssetRef,
    ):
        """Oracle B holds (sla, artifact_bytes, verdict_from_A).

        Oracle B has no access to A's private key. It can still verify
        A's verdict because `verify_signature` uses only the public key
        embedded in the verdict's `signature.signer` field.
        """
        artifact = json.dumps({"summary": "replay test"}).encode()
        sla = _make_sla_with_hash(usd, artifact)

        verdict_from_a = oracle_a.evaluate_tier0(sla, artifact)

        # Oracle B verifies A's verdict using the self-contained signature.
        verdict_from_a.verify_signature()

        # Oracle B can also issue its own independent verdict on the same
        # (sla, artifact) pair and it will differ in evaluator_did + signer.
        verdict_from_b = oracle_b.evaluate_tier0(sla, artifact)
        verdict_from_b.verify_signature()

        assert verdict_from_a.evaluator_did != verdict_from_b.evaluator_did
        assert verdict_from_a.signer != verdict_from_b.signer
        # Both agree on the content verdict.
        assert verdict_from_a.result == verdict_from_b.result

    def test_oracle_b_can_verify_tier3_override(
        self,
        oracle_a: Oracle,
        oracle_b: Oracle,
        usd: AssetRef,
        founder_keypair: Ed25519Keypair,
    ):
        """Oracle B can verify a Tier 3 verdict produced by Oracle A.

        The Tier 3 verdict is signed by the founder keypair; B does not
        need A's or the founder's private key - only the embedded pubkey.
        """
        prior, _, _ = _make_rejected_verdict(oracle_a, usd)
        override = oracle_a.founder_override(
            prior,
            result="accepted",
            reason="Replay test override.",
            founder_keypair=founder_keypair,
            founder_identity="riley",
        )
        # Oracle B verifies without needing A's or founder's private key.
        override.verify_signature()

    def test_verdicts_from_a_and_b_have_same_result_on_same_artifact(
        self,
        oracle_a: Oracle,
        oracle_b: Oracle,
        usd: AssetRef,
    ):
        """Independent evaluations of the same (sla, artifact) agree on result."""
        artifact = json.dumps({"summary": "consistency check"}).encode()
        sla = _make_sla_with_hash(usd, artifact)

        va = oracle_a.evaluate_tier0(sla, artifact)
        vb = oracle_b.evaluate_tier0(sla, artifact)

        assert va.result == vb.result == "accepted"
        assert va.evidence["kind"] == vb.evidence["kind"] == "schema_pass"
        # Artifact hash is deterministic.
        assert va.artifact_hash == vb.artifact_hash
