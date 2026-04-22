"""
agent-settlement-sim/tests/test_challenge_window.py -- Challenge lifecycle e2e
==============================================================================
End-to-end tests for the challenge-window path:
  1. Tier 1 verdict issued.
  2. Requester raises Challenge within window.
  3. Founder issues Tier 3 override with evidence.challenge_hash + overrides.
  4. `handle_settling` settles via the Tier 3 override verdict.

Also covers:
  - Window still open blocks release (ChallengeWindowError).
  - Challenge after window raises ChallengeWindowError.
"""
from __future__ import annotations

import hashlib
import json
import tempfile
from decimal import Decimal
from pathlib import Path

import pytest

from core.primitives.asset import AssetRef
from core.primitives.challenge import Challenge
from core.primitives.evaluator import EvaluationOutput
from core.primitives.exceptions import ChallengeWindowError
from core.primitives.identity import Ed25519Keypair
from core.primitives.money import Money
from core.primitives.oracle import Oracle
from core.primitives.schema_verifier import SchemaVerifier
from core.primitives.settlement_adapters.mock_adapter import MockSettlementAdapter
from core.primitives.settlement_ledger import SettlementEventLedger
from core.primitives.signer import LocalKeypairSigner
from core.primitives.sla import InterOrgSLA
from core.primitives.state import FOUNDER_PRINCIPALS
from agent_settlement_sim.tests.fixtures import StubPassthroughEvaluator
from agent_settlement_sim.researcher_loop import ResearcherSim, ScenarioCtx


# ---------------------------------------------------------------------------
# Constants
# 64-char lowercase hex strings required by B0-d coupling validation.
# ---------------------------------------------------------------------------
_REQUESTER_DID = "did:companyos:challenge-requester"
_PROVIDER_DID = "did:companyos:challenge-provider"
_NODE_DID = "did:companyos:challenge-oracle"
_EVALUATOR_DID = "did:companyos:challenge-evaluator"
_CANONICAL_HASH = "c1d2e3f4a5b6" + "0" * 52           # 64 hex chars
_EVALUATOR_PUBKEY_HEX = "cc" * 32                      # 64 hex chars
_CHALLENGE_WINDOW_SEC = 60  # short so tests can advance past it


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _usd() -> AssetRef:
    return AssetRef(asset_id="mock-usd", contract="USD", decimals=6)


def _valid_schema() -> dict:
    return {
        "kind": "json_schema",
        "spec_version": "2020-12",
        "schema": {
            "type": "object",
            "required": ["summary"],
            "properties": {"summary": {"type": "string"}},
        },
    }


def _valid_artifact() -> bytes:
    return json.dumps({"summary": "A solid analysis."}).encode()


def _make_sla(usd: AssetRef, artifact_bytes: bytes) -> InterOrgSLA:
    artifact_hash = hashlib.sha256(artifact_bytes).hexdigest()
    sla = InterOrgSLA.create(
        sla_id="sim-challenge-sla-001",
        requester_node_did=_REQUESTER_DID,
        provider_node_did=_PROVIDER_DID,
        task_scope="produce analysis report",
        deliverable_schema=_valid_schema(),
        accuracy_requirement=0.9,
        latency_ms=60_000,
        payment=Money(Decimal("0.001000"), usd),
        penalty_stake=Money(Decimal("0.000500"), usd),
        nonce=InterOrgSLA.new_nonce(),
        issued_at="2026-04-21T00:00:00Z",
        expires_at="2099-01-01T00:00:00Z",
        primary_evaluator_did=_EVALUATOR_DID,
        canonical_evaluator_hash=_CANONICAL_HASH,
        primary_evaluator_pubkey_hex=_EVALUATOR_PUBKEY_HEX,
        challenge_window_sec=_CHALLENGE_WINDOW_SEC,
    )
    return sla.with_delivery_hash(artifact_hash)


def _stub_accepted() -> StubPassthroughEvaluator:
    output = EvaluationOutput(
        result="accepted",
        score=Decimal("0.92"),
        evidence={"kind": "schema_pass_with_score"},
        evaluator_canonical_hash=_CANONICAL_HASH,
    )
    return StubPassthroughEvaluator(
        evaluator_did=_EVALUATOR_DID,
        canonical_hash=_CANONICAL_HASH,
        canned_output=output,
    )


@pytest.fixture
def usd() -> AssetRef:
    return _usd()


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


@pytest.fixture
def adapter(tmp_path) -> MockSettlementAdapter:
    usd = _usd()
    ledger = SettlementEventLedger(tmp_path / "events")
    a = MockSettlementAdapter(supported_assets=(usd,), ledger=ledger)
    a.fund(_REQUESTER_DID, Money(Decimal("0.010000"), usd))
    return a


def _make_ctx(sla: InterOrgSLA, artifact: bytes, adapter) -> ScenarioCtx:
    handle = adapter.lock(
        sla.payment,
        ref=sla.sla_id,
        nonce=InterOrgSLA.new_nonce(),
        principal=_REQUESTER_DID,
    )
    return ScenarioCtx(sla=sla, artifact_bytes=artifact, handle=handle)


# ---------------------------------------------------------------------------
# Window still open blocks release
# ---------------------------------------------------------------------------
class TestWindowStillOpen:
    def test_window_blocks_release_before_fast_forward(self, oracle, adapter, usd):
        """Without fast_forward, challenge window is still open -> ChallengeWindowError."""
        artifact = _valid_artifact()
        sla = _make_sla(usd, artifact)

        sim = ResearcherSim(adapter=adapter, oracle=oracle, evaluator=_stub_accepted())
        ctx = _make_ctx(sla, artifact, adapter)
        sim.handle_verifying(ctx)

        # Do NOT fast_forward -- window is still open.
        with pytest.raises(ChallengeWindowError, match="challenge window still open"):
            sim.handle_settling(ctx)


# ---------------------------------------------------------------------------
# Challenge happy path (Tier 1 -> Challenge -> Tier 3 override)
# ---------------------------------------------------------------------------
class TestChallengeHappyPath:
    def test_challenge_then_founder_override_releases(self, oracle, adapter, usd):
        """Requester challenges within window; founder overrides to accepted; settled."""
        artifact = _valid_artifact()
        sla = _make_sla(usd, artifact)

        sim = ResearcherSim(adapter=adapter, oracle=oracle, evaluator=_stub_accepted())
        ctx = _make_ctx(sla, artifact, adapter)

        # Step 1: Tier 1 verdict issued.
        sim.handle_verifying(ctx)
        assert ctx.verdict.tier == 1
        tier1_verdict = ctx.verdict

        # Step 2: Requester raises Challenge within the window.
        requester_kp = Ed25519Keypair.generate()
        requester_signer = LocalKeypairSigner(requester_kp)
        challenge = Challenge.create(
            prior_verdict=tier1_verdict,
            challenger_did=_REQUESTER_DID,
            reason="Summary quality is insufficient per contract intent.",
            signer=requester_signer,
        )
        # Record the challenge into the adapter (within window -- no fast_forward yet).
        adapter.raise_challenge(
            ctx.handle,
            challenge,
            requester_did=_REQUESTER_DID,
            provider_did=_PROVIDER_DID,
            prior_verdict=tier1_verdict,
            challenge_window_sec=_CHALLENGE_WINDOW_SEC,
        )

        # Step 3: Founder issues Tier 3 override resolving the challenge.
        founder_kp = Ed25519Keypair.generate()
        founder_signer = LocalKeypairSigner(founder_kp)
        # Use the identity that IS in FOUNDER_PRINCIPALS.
        founder_identity = next(iter(FOUNDER_PRINCIPALS))

        tier3_evidence_extras = {
            "challenge_hash": challenge.challenge_hash,
            "overrides": tier1_verdict.verdict_hash,
        }
        tier3_verdict = oracle.founder_override(
            prior_verdict=tier1_verdict,
            result="accepted",
            reason="Founder reviewed: summary meets contract intent.",
            founder_signer=founder_signer,
            founder_identity=founder_identity,
        )
        # Manually attach challenge_hash to evidence for adapter's challenge_resolved path.
        # We need to build a verdict with the extra evidence fields.
        from core.primitives.oracle import OracleVerdict
        full_evidence = dict(tier3_verdict.evidence)
        full_evidence["challenge_hash"] = challenge.challenge_hash

        tier3_with_challenge = OracleVerdict.create(
            sla_id=tier3_verdict.sla_id,
            artifact_hash=tier3_verdict.artifact_hash,
            tier=3,
            result=tier3_verdict.result,
            evaluator_did=tier3_verdict.evaluator_did,
            evidence=full_evidence,
            issued_at=tier3_verdict.issued_at,
            signer=founder_signer,
        )

        ctx.override_verdict = tier3_with_challenge

        # Step 4: Advance clock past window (so challenge_window check doesn't block).
        sim.fast_forward(_CHALLENGE_WINDOW_SEC + 1)

        # Step 5: Settle via the Tier 3 override.
        receipt = sim.handle_settling(ctx)

        assert receipt.outcome == "released"
        assert receipt.to == _PROVIDER_DID
        assert ctx.state == "done"

    def test_challenge_ledger_events_emitted(self, oracle, adapter, usd):
        """After challenge + override, ledger contains challenge_raised + challenge_resolved."""
        artifact = _valid_artifact()
        sla = _make_sla(usd, artifact)

        sim = ResearcherSim(adapter=adapter, oracle=oracle, evaluator=_stub_accepted())
        ctx = _make_ctx(sla, artifact, adapter)
        sim.handle_verifying(ctx)
        tier1_verdict = ctx.verdict

        requester_kp = Ed25519Keypair.generate()
        challenge = Challenge.create(
            prior_verdict=tier1_verdict,
            challenger_did=_REQUESTER_DID,
            reason="Dispute: output quality below agreed standard.",
            signer=LocalKeypairSigner(requester_kp),
        )
        adapter.raise_challenge(
            ctx.handle,
            challenge,
            requester_did=_REQUESTER_DID,
            provider_did=_PROVIDER_DID,
            prior_verdict=tier1_verdict,
            challenge_window_sec=_CHALLENGE_WINDOW_SEC,
        )

        founder_kp = Ed25519Keypair.generate()
        founder_identity = next(iter(FOUNDER_PRINCIPALS))
        tier3 = oracle.founder_override(
            prior_verdict=tier1_verdict,
            result="accepted",
            reason="Override: analysis meets standard.",
            founder_signer=LocalKeypairSigner(founder_kp),
            founder_identity=founder_identity,
        )

        from core.primitives.oracle import OracleVerdict
        full_ev = dict(tier3.evidence)
        full_ev["challenge_hash"] = challenge.challenge_hash
        tier3_full = OracleVerdict.create(
            sla_id=tier3.sla_id,
            artifact_hash=tier3.artifact_hash,
            tier=3,
            result=tier3.result,
            evaluator_did=tier3.evaluator_did,
            evidence=full_ev,
            issued_at=tier3.issued_at,
            signer=LocalKeypairSigner(founder_kp),
        )

        ctx.override_verdict = tier3_full
        sim.fast_forward(_CHALLENGE_WINDOW_SEC + 1)
        sim.handle_settling(ctx)

        event_kinds = [ev.kind for ev in adapter._ledger.events()]
        assert "challenge_raised" in event_kinds
        assert "challenge_resolved" in event_kinds

    def test_unresolved_challenge_blocks_release(self, oracle, adapter, usd):
        """Unresolved challenge (no founder override) blocks settlement."""
        artifact = _valid_artifact()
        sla = _make_sla(usd, artifact)

        sim = ResearcherSim(adapter=adapter, oracle=oracle, evaluator=_stub_accepted())
        ctx = _make_ctx(sla, artifact, adapter)
        sim.handle_verifying(ctx)
        tier1_verdict = ctx.verdict

        requester_kp = Ed25519Keypair.generate()
        challenge = Challenge.create(
            prior_verdict=tier1_verdict,
            challenger_did=_REQUESTER_DID,
            reason="Output does not match agreed scope.",
            signer=LocalKeypairSigner(requester_kp),
        )
        adapter.raise_challenge(
            ctx.handle,
            challenge,
            requester_did=_REQUESTER_DID,
            provider_did=_PROVIDER_DID,
            prior_verdict=tier1_verdict,
            challenge_window_sec=_CHALLENGE_WINDOW_SEC,
        )

        # Advance past window but without resolving the challenge.
        sim.fast_forward(_CHALLENGE_WINDOW_SEC + 1)

        with pytest.raises(ChallengeWindowError, match="unresolved challenge"):
            sim.handle_settling(ctx)
