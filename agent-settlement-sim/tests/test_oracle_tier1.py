"""
agent-settlement-sim/tests/test_oracle_tier1.py -- Sim Tier 1 end-to-end
=========================================================================
End-to-end sim tests for the Tier 1 happy path, rejection path, and timeout
path through the full `ResearcherSim` state machine + `MockSettlementAdapter`.

These tests wire:
  - `ResearcherSim` with a `StubPassthroughEvaluator`
  - `MockSettlementAdapter` + `SettlementEventLedger`
  - `Oracle.evaluate_tier1` dispatched from `handle_verifying`
  - `release_pending_verdict` with challenge window + `fast_forward`

Coverage:
- Tier 1 happy path: score 0.92 >= requirement 0.9, verdict accepted,
  fast_forward past window, escrow released to provider.
- Tier 1 rejected: score 0.70 < requirement 0.9, verdict rejected,
  fast_forward past window, escrow slashed back to requester.
- Tier 1 timeout: evaluator sleeps > evaluator_timeout_sec, refunded
  verdict with evidence.kind = "evaluator_timeout", settled as refund.
"""
from __future__ import annotations

import hashlib
import json
import tempfile
import time
from decimal import Decimal
from pathlib import Path

import pytest

from core.primitives.asset import AssetRegistry, AssetRef
from core.primitives.evaluator import EvaluationOutput
from core.primitives.identity import Ed25519Keypair
from core.primitives.money import Money
from core.primitives.oracle import Oracle
from core.primitives.schema_verifier import SchemaVerifier
from core.primitives.settlement_adapters.mock_adapter import MockSettlementAdapter
from core.primitives.settlement_ledger import SettlementEventLedger
from core.primitives.sla import InterOrgSLA
from agent_settlement_sim.tests.fixtures import StubPassthroughEvaluator
from agent_settlement_sim.researcher_loop import ResearcherSim, ScenarioCtx


# ---------------------------------------------------------------------------
# Constants
# 64-char lowercase hex strings required by the B0-d coupling validation in
# InterOrgSLA.create: canonical_evaluator_hash and primary_evaluator_pubkey_hex
# must be exactly 64 lowercase hex characters when set.
# ---------------------------------------------------------------------------
_REQUESTER_DID = "did:companyos:sim-requester"
_PROVIDER_DID = "did:companyos:sim-provider"
_NODE_DID = "did:companyos:sim-oracle-node"
_EVALUATOR_DID = "did:companyos:sim-evaluator"
_CANONICAL_HASH = "a1b2c3d4e5f6" + "0" * 52           # 64 hex chars
_EVALUATOR_PUBKEY_HEX = "bb" * 32                      # 64 hex chars
_CHALLENGE_WINDOW_SEC = 60  # short window for tests


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
            "properties": {
                "summary": {"type": "string"},
            },
        },
    }


def _valid_artifact() -> bytes:
    return json.dumps({"summary": "A thorough analysis."}).encode()


def _make_sla(usd: AssetRef, artifact_bytes: bytes) -> InterOrgSLA:
    artifact_hash = hashlib.sha256(artifact_bytes).hexdigest()
    sla = InterOrgSLA.create(
        sla_id="sim-tier1-sla-001",
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


def _stub(result: str, score: Decimal) -> StubPassthroughEvaluator:
    output = EvaluationOutput(
        result=result,
        score=score,
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
    adapter = MockSettlementAdapter(supported_assets=(usd,), ledger=ledger)
    adapter.fund(_REQUESTER_DID, Money(Decimal("0.010000"), usd))
    return adapter


def _make_ctx(sla: InterOrgSLA, artifact_bytes: bytes, adapter) -> ScenarioCtx:
    usd = _usd()
    handle = adapter.lock(
        sla.payment,
        ref=sla.sla_id,
        nonce=InterOrgSLA.new_nonce(),
        principal=_REQUESTER_DID,
    )
    return ScenarioCtx(sla=sla, artifact_bytes=artifact_bytes, handle=handle)


# ---------------------------------------------------------------------------
# Tier 1 happy path
# ---------------------------------------------------------------------------
class TestSimTier1HappyPath:
    def test_accepted_after_window(self, oracle, adapter, usd, tmp_path):
        """Score 0.92 >= 0.9 requirement; fast-forward past window; escrow released."""
        artifact = _valid_artifact()
        sla = _make_sla(usd, artifact)
        evaluator = _stub("accepted", Decimal("0.92"))

        sim = ResearcherSim(adapter=adapter, oracle=oracle, evaluator=evaluator)
        ctx = _make_ctx(sla, artifact, adapter)

        # Step 1: verify -> Tier 1 verdict issued.
        sim.handle_verifying(ctx)
        assert ctx.verdict is not None
        assert ctx.verdict.tier == 1
        assert ctx.verdict.result == "accepted"
        assert ctx.verdict.score == Decimal("0.92")

        # Step 2: advance clock past challenge window.
        sim.fast_forward(_CHALLENGE_WINDOW_SEC + 1)

        # Step 3: settle.
        receipt = sim.handle_settling(ctx)
        assert receipt.outcome == "released"
        assert receipt.to == _PROVIDER_DID
        assert ctx.state == "done"

    def test_verdict_signature_valid(self, oracle, adapter, usd):
        """Tier 1 verdict signature is valid before settling."""
        artifact = _valid_artifact()
        sla = _make_sla(usd, artifact)
        evaluator = _stub("accepted", Decimal("0.92"))

        sim = ResearcherSim(adapter=adapter, oracle=oracle, evaluator=evaluator)
        ctx = _make_ctx(sla, artifact, adapter)
        sim.handle_verifying(ctx)

        # Must not raise.
        ctx.verdict.verify_signature()

    def test_provider_balance_increases(self, oracle, adapter, usd):
        """After acceptance, provider's balance grows by payment amount."""
        artifact = _valid_artifact()
        sla = _make_sla(usd, artifact)
        evaluator = _stub("accepted", Decimal("0.92"))

        sim = ResearcherSim(adapter=adapter, oracle=oracle, evaluator=evaluator)
        ctx = _make_ctx(sla, artifact, adapter)
        sim.handle_verifying(ctx)
        sim.fast_forward(_CHALLENGE_WINDOW_SEC + 1)
        sim.handle_settling(ctx)

        provider_balance = adapter.balance(_PROVIDER_DID, usd)
        assert provider_balance.quantity == sla.payment.quantity


# ---------------------------------------------------------------------------
# Tier 1 rejected path
# ---------------------------------------------------------------------------
class TestSimTier1Rejected:
    def test_rejected_after_window_slashes_escrow(self, oracle, adapter, usd):
        """Score 0.70 < 0.9; fast-forward past window; escrow slashed to requester."""
        artifact = _valid_artifact()
        sla = _make_sla(usd, artifact)
        evaluator = _stub("rejected", Decimal("0.70"))

        sim = ResearcherSim(adapter=adapter, oracle=oracle, evaluator=evaluator)
        ctx = _make_ctx(sla, artifact, adapter)

        sim.handle_verifying(ctx)
        assert ctx.verdict.result == "rejected"

        sim.fast_forward(_CHALLENGE_WINDOW_SEC + 1)
        receipt = sim.handle_settling(ctx)

        assert receipt.outcome == "slashed"
        assert receipt.to == _REQUESTER_DID


# ---------------------------------------------------------------------------
# Tier 1 timeout path
# ---------------------------------------------------------------------------
class TestSimTier1Timeout:
    def test_timeout_refund_via_sim(self, adapter, usd):
        """Evaluator sleeps > timeout; Oracle returns refund; sim settles refund."""
        artifact = _valid_artifact()
        sla = _make_sla(usd, artifact)

        # Oracle with very short timeout.
        node_kp = Ed25519Keypair.generate()
        fast_oracle = Oracle(
            node_did=_NODE_DID,
            node_keypair=node_kp,
            schema_verifier=SchemaVerifier(),
            evaluator_timeout_sec=1,
        )

        class SlowEvaluator(StubPassthroughEvaluator):
            def evaluate(self, sla, artifact_bytes, *, artifact_properties=None):
                time.sleep(10)  # well beyond 1s
                return super().evaluate(sla, artifact_bytes)

        slow_eval = SlowEvaluator(
            evaluator_did=_EVALUATOR_DID,
            canonical_hash=_CANONICAL_HASH,
            canned_output=EvaluationOutput(
                result="accepted",
                score=Decimal("0.9"),
                evidence={"kind": "schema_pass_with_score"},
                evaluator_canonical_hash=_CANONICAL_HASH,
            ),
        )

        sim = ResearcherSim(adapter=adapter, oracle=fast_oracle, evaluator=slow_eval)
        ctx = _make_ctx(sla, artifact, adapter)

        sim.handle_verifying(ctx)

        assert ctx.verdict.result == "refunded"
        assert ctx.verdict.evidence["kind"] == "evaluator_timeout"

        # Refund path -- no need to advance clock (Tier 1 without challenge_window_sec
        # check for refunded result). Actually, the adapter checks window for tier 1.
        # Advance clock past window so settlement doesn't block.
        sim.fast_forward(_CHALLENGE_WINDOW_SEC + 1)

        receipt = sim.handle_settling(ctx)
        # Refunded outcome returns funds to original locker.
        assert receipt.outcome == "released"

    def test_timeout_evidence_kind(self, adapter, usd):
        """Timeout verdict carries evidence.kind = 'evaluator_timeout'."""
        artifact = _valid_artifact()
        sla = _make_sla(usd, artifact)

        node_kp = Ed25519Keypair.generate()
        fast_oracle = Oracle(
            node_did=_NODE_DID,
            node_keypair=node_kp,
            schema_verifier=SchemaVerifier(),
            evaluator_timeout_sec=1,
        )

        class SlowEvaluator(StubPassthroughEvaluator):
            def evaluate(self, sla, artifact_bytes, *, artifact_properties=None):
                time.sleep(10)
                return super().evaluate(sla, artifact_bytes)

        slow_eval = SlowEvaluator(
            evaluator_did=_EVALUATOR_DID,
            canonical_hash=_CANONICAL_HASH,
            canned_output=EvaluationOutput(
                result="accepted",
                score=Decimal("0.9"),
                evidence={"kind": "schema_pass_with_score"},
                evaluator_canonical_hash=_CANONICAL_HASH,
            ),
        )

        sim = ResearcherSim(adapter=adapter, oracle=fast_oracle, evaluator=slow_eval)
        ctx = _make_ctx(sla, artifact, adapter)
        sim.handle_verifying(ctx)

        assert ctx.verdict.evidence["kind"] == "evaluator_timeout"
        assert ctx.verdict.score == Decimal("0")
