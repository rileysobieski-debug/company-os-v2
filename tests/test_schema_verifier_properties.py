"""
tests/test_schema_verifier_properties.py -- Hypothesis properties for SchemaVerifier
======================================================================================
Ticket A1 property-based tests.

Properties verified:
1. Determinism: verify(sla, artifact) called twice on the same inputs in the
   same process produces byte-identical (result, evidence) tuples.
   Rationale: the spec requires pure-function behavior; this test enforces
   that no hidden state (e.g. random seed, clock, global counter) leaks in.

2. Result is always a valid OracleResult ("accepted" | "rejected" | "refunded").

3. Evidence always contains a "kind" key drawn from EvidenceKind.

Strategy design
---------------
We generate valid (sla, artifact_bytes) pairs by:
- Drawing a JSON-serializable object payload (dict with string keys, primitive values).
- Drawing a JSON Schema that can be applied to such objects.
- We use two sub-strategies: one where the schema always accepts the payload
  (happy-path), and one where the payload may or may not satisfy the schema.

For the malformed / missing schema cases we draw from a fixture library rather
than trying to generate them via Hypothesis (schema validity is complex to
generate negatively).
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from core.primitives.asset import AssetRef
from core.primitives.money import Money
from core.primitives.oracle import EvidenceKind
from core.primitives.schema_verifier import SchemaVerifier
from core.primitives.sla import InterOrgSLA

# Valid EvidenceKind values (for assertion checks)
_VALID_EVIDENCE_KINDS: frozenset[str] = frozenset(
    [
        "schema_pass",
        "schema_fail",
        "hash_mismatch",
        "artifact_parse_error",
        "sla_schema_malformed",
        "sla_missing_schema",
        "unsupported_schema_kind",
        "unsupported_schema_version",
        "founder_override",
    ]
)
_VALID_RESULTS: frozenset[str] = frozenset(["accepted", "rejected", "refunded"])

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------
_USD = AssetRef(asset_id="mock-usd", contract="USD", decimals=6)
_MONEY = Money(quantity=Decimal("50.000000"), asset=_USD)
_VERIFIER = SchemaVerifier()


def _make_sla(schema_envelope: dict, artifact_hash: str) -> InterOrgSLA:
    sla = InterOrgSLA.create(
        sla_id="prop-sla-001",
        requester_node_did="did:companyos:req",
        provider_node_did="did:companyos:prov",
        task_scope="property test task",
        deliverable_schema=schema_envelope,
        accuracy_requirement=0.8,
        latency_ms=30_000,
        payment=_MONEY,
        penalty_stake=_MONEY,
        nonce=InterOrgSLA.new_nonce(),
        issued_at="2026-04-21T00:00:00Z",
        expires_at="2026-04-28T00:00:00Z",
    )
    return sla.with_delivery_hash(artifact_hash)


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------
_JSON_PRIMITIVE = st.one_of(
    st.text(max_size=20),
    st.integers(min_value=-1000, max_value=1000),
    st.booleans(),
    st.none(),
)

_JSON_LEAF_DICT = st.dictionaries(
    keys=st.text(alphabet=st.characters(whitelist_categories=("L",)), min_size=1, max_size=8),
    values=_JSON_PRIMITIVE,
    max_size=5,
)


@st.composite
def _valid_sla_and_artifact(draw: Any):
    """Draw a (sla, artifact_bytes) pair where the schema accepts the artifact."""
    # Draw a simple dict payload
    payload: dict = draw(_JSON_LEAF_DICT)
    artifact_bytes = json.dumps(payload, sort_keys=True).encode("utf-8")
    artifact_hash = hashlib.sha256(artifact_bytes).hexdigest()

    # Build a permissive schema that accepts any object (no constraints)
    schema_envelope = {
        "kind": "json_schema",
        "spec_version": "2020-12",
        "schema": {"type": "object"},
    }
    sla = _make_sla(schema_envelope, artifact_hash)
    return sla, artifact_bytes


@st.composite
def _arbitrary_sla_and_artifact(draw: Any):
    """Draw a (sla, artifact_bytes) pair; schema may or may not match payload.

    Uses a constrained-type schema so we get both pass and fail cases.
    """
    payload: dict = draw(_JSON_LEAF_DICT)
    artifact_bytes = json.dumps(payload, sort_keys=True).encode("utf-8")
    artifact_hash = hashlib.sha256(artifact_bytes).hexdigest()

    # Randomly constrain the schema: either permissive or string-only
    require_string_values = draw(st.booleans())
    if require_string_values:
        schema_envelope = {
            "kind": "json_schema",
            "spec_version": "2020-12",
            "schema": {
                "type": "object",
                "additionalProperties": {"type": "string"},
            },
        }
    else:
        schema_envelope = {
            "kind": "json_schema",
            "spec_version": "2020-12",
            "schema": {"type": "object"},
        }

    sla = _make_sla(schema_envelope, artifact_hash)
    return sla, artifact_bytes


# ---------------------------------------------------------------------------
# Property 1: determinism on valid (sla, artifact) pairs
# ---------------------------------------------------------------------------
@given(inputs=_valid_sla_and_artifact())
@settings(
    max_examples=100,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_verify_deterministic_same_inputs(inputs: tuple) -> None:
    """verify(sla, artifact) is deterministic: two calls return identical tuples."""
    sla, artifact_bytes = inputs
    result1, evidence1 = _VERIFIER.verify(sla, artifact_bytes)
    result2, evidence2 = _VERIFIER.verify(sla, artifact_bytes)

    assert result1 == result2, (
        f"Non-deterministic result: first={result1!r}, second={result2!r}"
    )
    assert evidence1 == evidence2, (
        f"Non-deterministic evidence:\nfirst={evidence1!r}\nsecond={evidence2!r}"
    )
    # Byte-identical check: serialize both evidence dicts the same way
    assert (
        json.dumps(evidence1, sort_keys=True) == json.dumps(evidence2, sort_keys=True)
    ), "Evidence dicts serialize to different bytes"


# ---------------------------------------------------------------------------
# Property 2: result is always a valid OracleResult
# ---------------------------------------------------------------------------
@given(inputs=_arbitrary_sla_and_artifact())
@settings(
    max_examples=100,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_verify_result_always_valid_oracle_result(inputs: tuple) -> None:
    """verify() always returns a valid OracleResult string."""
    sla, artifact_bytes = inputs
    result, _ = _VERIFIER.verify(sla, artifact_bytes)
    assert result in _VALID_RESULTS, f"Unknown result: {result!r}"


# ---------------------------------------------------------------------------
# Property 3: evidence always has a valid 'kind'
# ---------------------------------------------------------------------------
@given(inputs=_arbitrary_sla_and_artifact())
@settings(
    max_examples=100,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_verify_evidence_always_has_valid_kind(inputs: tuple) -> None:
    """verify() always returns evidence with a valid 'kind' key."""
    sla, artifact_bytes = inputs
    _, evidence = _VERIFIER.verify(sla, artifact_bytes)
    assert "kind" in evidence, f"evidence missing 'kind': {evidence!r}"
    assert evidence["kind"] in _VALID_EVIDENCE_KINDS, (
        f"Unknown evidence kind: {evidence['kind']!r}"
    )


# ---------------------------------------------------------------------------
# Property 4: determinism across arbitrary (sla, artifact) pairs
# ---------------------------------------------------------------------------
@given(inputs=_arbitrary_sla_and_artifact())
@settings(
    max_examples=80,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_verify_deterministic_arbitrary_inputs(inputs: tuple) -> None:
    """verify is deterministic for arbitrary (sla, artifact) pairs, not just happy path."""
    sla, artifact_bytes = inputs
    result1, evidence1 = _VERIFIER.verify(sla, artifact_bytes)
    result2, evidence2 = _VERIFIER.verify(sla, artifact_bytes)
    assert result1 == result2
    assert json.dumps(evidence1, sort_keys=True) == json.dumps(evidence2, sort_keys=True)


# ---------------------------------------------------------------------------
# Property 5: accepted verdicts always have schema_pass evidence
# ---------------------------------------------------------------------------
@given(inputs=_valid_sla_and_artifact())
@settings(
    max_examples=60,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_accepted_always_has_schema_pass_evidence(inputs: tuple) -> None:
    """When result is 'accepted', evidence.kind must be 'schema_pass'."""
    sla, artifact_bytes = inputs
    result, evidence = _VERIFIER.verify(sla, artifact_bytes)
    if result == "accepted":
        assert evidence["kind"] == "schema_pass"
