"""tests/test_oracle_docs.py -- A7 doc-example runner for ORACLE.md

Executes the runnable code blocks from core/primitives/ORACLE.md verbatim
(see the test_settlement_docs.py precedent). If the docs drift from the
implementation, these tests fail loudly.
"""
from __future__ import annotations

import hashlib
import json
import tempfile
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
ASSET_REGISTRY_DIR = REPO_ROOT / "core" / "primitives" / "asset_registry"


def test_oracle_md_tier0_happy_path(tmp_path) -> None:
    """Runs the Tier 0 happy-path block from ORACLE.md section (a)."""
    from core.primitives import (
        AssetRegistry, Money, AdapterRegistry, MockSettlementAdapter,
        InterOrgSLA, Ed25519Keypair, NodeRegistry, SettlementEventLedger,
    )
    from core.primitives.oracle import Oracle
    from core.primitives.schema_verifier import SchemaVerifier

    # 1. Setup
    asset_reg = AssetRegistry()
    asset_reg.load(ASSET_REGISTRY_DIR)
    usd = asset_reg.get("mock-usd")

    adapters = AdapterRegistry(asset_reg)
    mock = MockSettlementAdapter(
        supported_assets=(usd,),
        ledger=SettlementEventLedger(tmp_path / "events"),
    )
    adapters.register(mock)

    nodes = NodeRegistry()
    nodes.load(tmp_path / "nodes")

    req_kp = Ed25519Keypair.generate()
    prov_kp = Ed25519Keypair.generate()
    nodes.register("did:companyos:req", req_kp.public_key)
    nodes.register("did:companyos:prov", prov_kp.public_key)

    # 2. Fund requester and build the SLA.
    mock.fund("did:companyos:req", Money(Decimal("0.010000"), usd))
    deliverable_schema = {
        "kind": "json_schema",
        "spec_version": "2020-12",
        "schema": {
            "type": "object",
            "required": ["result", "quality_score"],
            "properties": {
                "result": {"type": "string"},
                "quality_score": {"type": "number", "minimum": 0, "maximum": 1},
            },
        },
    }
    sla = InterOrgSLA.create(
        sla_id="sla-oracle-demo",
        requester_node_did="did:companyos:req",
        provider_node_did="did:companyos:prov",
        task_scope="summarize the 10-K",
        deliverable_schema=deliverable_schema,
        accuracy_requirement=0.9,
        latency_ms=120_000,
        payment=Money(Decimal("0.001000"), usd),
        penalty_stake=Money(Decimal("0.000500"), usd),
        nonce=InterOrgSLA.new_nonce(),
        issued_at=datetime.now(timezone.utc),
        expires_at="2099-01-01T00:00:00Z",
    )

    # 3. Provider delivery + hash binding.
    artifact_bytes = json.dumps(
        {"result": "A one-page summary of the 10-K.", "quality_score": 0.97}
    ).encode("utf-8")
    artifact_hash = hashlib.sha256(artifact_bytes).hexdigest()
    sla = sla.with_delivery_hash(artifact_hash)

    # 4. Lock, evaluate, verify.
    handle = adapters.adapter_for(usd).lock(
        sla.payment,
        ref=sla.sla_id,
        nonce=InterOrgSLA.new_nonce(),
        principal="did:companyos:req",
    )
    oracle = Oracle(
        node_did="did:companyos:req",
        node_keypair=req_kp,
        schema_verifier=SchemaVerifier(),
    )
    verdict = oracle.evaluate_tier0(sla, artifact_bytes)
    assert verdict.result == "accepted"
    assert verdict.tier == 0
    verdict.verify_signature()

    # 5. Settle via the verdict.
    receipt = adapters.adapter_for(usd).release_pending_verdict(
        handle,
        verdict,
        expected_artifact_hash=sla.artifact_hash_at_delivery,
        requester_did=sla.requester_node_did,
        provider_did=sla.provider_node_did,
    )
    assert receipt.outcome == "released"
    assert receipt.to == "did:companyos:prov"


def test_oracle_md_tier3_override_snippet() -> None:
    """Runs the Tier 3 override snippet from ORACLE.md section (c).

    Uses an SLA whose schema the artifact fails so Tier 0 returns
    rejected, then exercises founder_override.
    """
    from core.primitives import (
        AssetRegistry, Money, InterOrgSLA, Ed25519Keypair,
    )
    from core.primitives.oracle import Oracle
    from core.primitives.schema_verifier import SchemaVerifier
    from core.primitives.exceptions import SignatureError

    asset_reg = AssetRegistry()
    asset_reg.load(ASSET_REGISTRY_DIR)
    usd = asset_reg.get("mock-usd")

    deliverable_schema = {
        "kind": "json_schema",
        "spec_version": "2020-12",
        "schema": {
            "type": "object",
            "required": ["result", "quality_score"],
            "properties": {
                "result": {"type": "string"},
                "quality_score": {"type": "number", "minimum": 0, "maximum": 1},
            },
        },
    }
    sla = InterOrgSLA.create(
        sla_id="sla-tier3-demo",
        requester_node_did="did:companyos:req",
        provider_node_did="did:companyos:prov",
        task_scope="summarize the 10-K",
        deliverable_schema=deliverable_schema,
        accuracy_requirement=0.9,
        latency_ms=120_000,
        payment=Money(Decimal("0.001000"), usd),
        penalty_stake=Money(Decimal("0.000500"), usd),
        nonce=InterOrgSLA.new_nonce(),
        issued_at=datetime.now(timezone.utc),
        expires_at="2099-01-01T00:00:00Z",
    )
    # Artifact is missing quality_score -> schema_fail -> rejected.
    artifact_bytes = json.dumps({"result": "short summary"}).encode("utf-8")
    sla = sla.with_delivery_hash(hashlib.sha256(artifact_bytes).hexdigest())

    req_kp = Ed25519Keypair.generate()
    oracle = Oracle(
        node_did="did:companyos:req",
        node_keypair=req_kp,
        schema_verifier=SchemaVerifier(),
    )
    tier0 = oracle.evaluate_tier0(sla, artifact_bytes)
    assert tier0.result == "rejected"

    founder_kp = Ed25519Keypair.generate()
    tier3 = oracle.founder_override(
        prior_verdict=tier0,
        result="accepted",
        reason="edge case: quality_score 0.899 rounds to 0.9 per convention",
        founder_keypair=founder_kp,
        founder_identity="founder",
    )
    assert tier3.tier == 3
    assert tier3.evidence["kind"] == "founder_override"
    assert tier3.evidence["overrides"] == tier0.verdict_hash
    assert tier3.evidence["reason"].startswith("edge case")
    tier3.verify_signature()

    # Non-founder identity is rejected.
    try:
        oracle.founder_override(
            prior_verdict=tier0,
            result="accepted",
            reason="oops",
            founder_keypair=Ed25519Keypair.generate(),
            founder_identity="mallory",
        )
    except SignatureError:
        pass
    else:  # pragma: no cover -- if no raise, test fails
        raise AssertionError("expected SignatureError for non-founder identity")
