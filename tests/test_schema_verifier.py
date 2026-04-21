"""
tests/test_schema_verifier.py -- Ticket A1 unit coverage for SchemaVerifier
===========================================================================
One test per row in the ruling-5 table, plus binary-artifact cases
and structural edge cases.

Ruling-5 table (all must be green):
  | Malformed JSON Schema in SLA        | refunded | sla_schema_malformed      |
  | Artifact fails JSON decode           | rejected | artifact_parse_error      |
  | SLA has no deliverable_schema field  | refunded | sla_missing_schema        |
  | Schema validation fails              | rejected | schema_fail               |
  | Schema validation passes             | accepted | schema_pass               |
  | Hash mismatch                        | rejected | hash_mismatch             |
  | Unknown kind                         | refunded | unsupported_schema_kind   |
  | Unknown spec_version                 | refunded | unsupported_schema_version|

Additional cases:
  - artifact_hash_at_delivery empty -> refunded / sla_missing_schema
  - binary artifact happy path (artifact_properties present)
  - binary artifact without artifact_properties -> refunded / sla_missing_schema
  - reserved kind "executable_tests" -> refunded / unsupported_schema_kind
  - reserved kind "composite" -> refunded / unsupported_schema_kind
  - hash mismatch happens BEFORE schema check (malformed schema + wrong hash)
  - empty deliverable_schema dict -> refunded / sla_missing_schema
  - schema missing "schema" key -> refunded / sla_schema_malformed
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Any

import pytest

from core.primitives.asset import AssetRef
from core.primitives.money import Money
from core.primitives.schema_verifier import SchemaVerifier
from core.primitives.sla import InterOrgSLA


# ---------------------------------------------------------------------------
# Helpers + fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def usd() -> AssetRef:
    return AssetRef(asset_id="mock-usd", contract="USD", decimals=6)


def _make_money(usd: AssetRef) -> Money:
    return Money(quantity=Decimal("100.000000"), asset=usd)


def _base_sla_kwargs(usd: AssetRef, schema_envelope: dict) -> dict:
    """Minimal valid kwargs for InterOrgSLA.create, with the given schema."""
    return {
        "sla_id": "test-sla-001",
        "requester_node_did": "did:companyos:requester",
        "provider_node_did": "did:companyos:provider",
        "task_scope": "deliver analysis report",
        "deliverable_schema": schema_envelope,
        "accuracy_requirement": 0.9,
        "latency_ms": 60_000,
        "payment": _make_money(usd),
        "penalty_stake": _make_money(usd),
        "nonce": InterOrgSLA.new_nonce(),
        "issued_at": "2026-04-21T00:00:00Z",
        "expires_at": "2026-04-28T00:00:00Z",
    }


def _valid_schema_envelope() -> dict:
    """A well-formed json_schema envelope for a JSON object artifact."""
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


def _make_sla_with_hash(
    usd: AssetRef,
    artifact_bytes: bytes,
    schema_envelope: dict | None = None,
) -> InterOrgSLA:
    """Create a fully-signed (hash-bound) SLA ready for verification."""
    if schema_envelope is None:
        schema_envelope = _valid_schema_envelope()
    sla = InterOrgSLA.create(**_base_sla_kwargs(usd, schema_envelope))
    artifact_hash = hashlib.sha256(artifact_bytes).hexdigest()
    return sla.with_delivery_hash(artifact_hash)


def _valid_artifact() -> bytes:
    """A JSON artifact that satisfies the valid schema envelope."""
    return json.dumps({"summary": "Analysis complete", "score": 0.95}).encode("utf-8")


@pytest.fixture(scope="module")
def verifier() -> SchemaVerifier:
    return SchemaVerifier()


# ---------------------------------------------------------------------------
# Ruling 5 -- all 8 outcome cases
# ---------------------------------------------------------------------------
class TestRuling5Table:
    """One test per row in the ruling-5 evidence table."""

    def test_accepted_schema_pass(self, verifier: SchemaVerifier, usd: AssetRef) -> None:
        """Schema validation passes -> accepted, schema_pass."""
        artifact = _valid_artifact()
        sla = _make_sla_with_hash(usd, artifact)
        result, evidence = verifier.verify(sla, artifact)
        assert result == "accepted"
        assert evidence["kind"] == "schema_pass"

    def test_rejected_schema_fail(self, verifier: SchemaVerifier, usd: AssetRef) -> None:
        """Schema validation fails -> rejected, schema_fail."""
        artifact = json.dumps({"score": 42}).encode("utf-8")  # missing required "summary"
        sla = _make_sla_with_hash(usd, artifact)
        result, evidence = verifier.verify(sla, artifact)
        assert result == "rejected"
        assert evidence["kind"] == "schema_fail"
        assert "detail" in evidence

    def test_rejected_artifact_parse_error(self, verifier: SchemaVerifier, usd: AssetRef) -> None:
        """Artifact fails JSON decode -> rejected, artifact_parse_error."""
        artifact = b"\xff\xfe not valid json"
        sla = _make_sla_with_hash(usd, artifact)
        result, evidence = verifier.verify(sla, artifact)
        assert result == "rejected"
        assert evidence["kind"] == "artifact_parse_error"

    def test_rejected_hash_mismatch(self, verifier: SchemaVerifier, usd: AssetRef) -> None:
        """Hash mismatch -> rejected, hash_mismatch."""
        artifact = _valid_artifact()
        sla = _make_sla_with_hash(usd, artifact)
        # Deliver a different artifact than the one the hash was bound to
        tampered = b'{"summary": "tampered"}'
        result, evidence = verifier.verify(sla, tampered)
        assert result == "rejected"
        assert evidence["kind"] == "hash_mismatch"
        assert "expected" in evidence
        assert "actual" in evidence
        assert evidence["expected"] != evidence["actual"]

    def test_refunded_sla_missing_schema(self, verifier: SchemaVerifier, usd: AssetRef) -> None:
        """SLA has no deliverable_schema field (empty dict) -> refunded, sla_missing_schema."""
        artifact = _valid_artifact()
        # Build SLA with empty deliverable_schema then manually set delivery hash
        # (empty schema dict is falsy -> triggers sla_missing_schema after hash check)
        sla = InterOrgSLA.create(**_base_sla_kwargs(usd, {}))
        artifact_hash = hashlib.sha256(artifact).hexdigest()
        sla = sla.with_delivery_hash(artifact_hash)
        result, evidence = verifier.verify(sla, artifact)
        assert result == "refunded"
        assert evidence["kind"] == "sla_missing_schema"

    def test_refunded_sla_schema_malformed(self, verifier: SchemaVerifier, usd: AssetRef) -> None:
        """Malformed JSON Schema in SLA -> refunded, sla_schema_malformed."""
        # "type": "not-a-real-type" raises SchemaError at validator build time
        bad_envelope = {
            "kind": "json_schema",
            "spec_version": "2020-12",
            "schema": {"type": "not-a-real-type"},
        }
        artifact = _valid_artifact()
        sla = _make_sla_with_hash(usd, artifact, bad_envelope)
        result, evidence = verifier.verify(sla, artifact)
        assert result == "refunded"
        assert evidence["kind"] == "sla_schema_malformed"

    def test_refunded_unsupported_schema_kind(self, verifier: SchemaVerifier, usd: AssetRef) -> None:
        """Unknown kind -> refunded, unsupported_schema_kind."""
        bad_envelope = {
            "kind": "totally_unknown",
            "spec_version": "2020-12",
            "schema": {},
        }
        artifact = _valid_artifact()
        sla = _make_sla_with_hash(usd, artifact, bad_envelope)
        result, evidence = verifier.verify(sla, artifact)
        assert result == "refunded"
        assert evidence["kind"] == "unsupported_schema_kind"

    def test_refunded_unsupported_schema_version(self, verifier: SchemaVerifier, usd: AssetRef) -> None:
        """Unknown spec_version -> refunded, unsupported_schema_version."""
        bad_envelope = {
            "kind": "json_schema",
            "spec_version": "draft-07",
            "schema": {"type": "object"},
        }
        artifact = _valid_artifact()
        sla = _make_sla_with_hash(usd, artifact, bad_envelope)
        result, evidence = verifier.verify(sla, artifact)
        assert result == "refunded"
        assert evidence["kind"] == "unsupported_schema_version"


# ---------------------------------------------------------------------------
# Binary artifact cases
# ---------------------------------------------------------------------------
class TestBinaryArtifact:
    """Binary artifact dispatch (artifact_format == "binary")."""

    def _binary_envelope(self) -> dict:
        """Schema envelope for a binary PDF artifact with a properties schema."""
        return {
            "kind": "json_schema",
            "spec_version": "2020-12",
            "artifact_format": "binary",
            "schema": {
                "type": "object",
                "properties": {
                    "page_count": {"type": "integer"},
                    "title": {"type": "string"},
                },
                "required": ["page_count"],
            },
        }

    def test_binary_artifact_happy_path(self, verifier: SchemaVerifier, usd: AssetRef) -> None:
        """Binary artifact with valid artifact_properties -> accepted."""
        # Raw bytes represent a non-JSON binary (e.g. a PDF stub)
        raw_bytes = b"%PDF-1.4 binary content..."
        sla = _make_sla_with_hash(usd, raw_bytes, self._binary_envelope())
        props = {"page_count": 12, "title": "Annual Report"}
        result, evidence = verifier.verify(sla, raw_bytes, artifact_properties=props)
        assert result == "accepted"
        assert evidence["kind"] == "schema_pass"

    def test_binary_artifact_missing_required_property(
        self, verifier: SchemaVerifier, usd: AssetRef
    ) -> None:
        """Binary artifact with artifact_properties that fails schema -> rejected."""
        raw_bytes = b"%PDF-1.4 binary content..."
        sla = _make_sla_with_hash(usd, raw_bytes, self._binary_envelope())
        # Missing required "page_count"
        props = {"title": "Annual Report"}
        result, evidence = verifier.verify(sla, raw_bytes, artifact_properties=props)
        assert result == "rejected"
        assert evidence["kind"] == "schema_fail"

    def test_binary_artifact_without_artifact_properties(
        self, verifier: SchemaVerifier, usd: AssetRef
    ) -> None:
        """Binary artifact without artifact_properties -> refunded, sla_missing_schema."""
        raw_bytes = b"%PDF-1.4 binary content..."
        sla = _make_sla_with_hash(usd, raw_bytes, self._binary_envelope())
        result, evidence = verifier.verify(sla, raw_bytes)  # no artifact_properties
        assert result == "refunded"
        assert evidence["kind"] == "sla_missing_schema"
        assert "binary artifact" in evidence["detail"]

    def test_non_binary_sla_with_bad_bytes_but_properties_still_rejects(
        self, verifier: SchemaVerifier, usd: AssetRef
    ) -> None:
        """Regression: the deleted fuzzy fallback.

        If the SLA does NOT declare artifact_format="binary", JSON is
        strictly expected. A provider that ships junk bytes plus a
        conveniently-valid artifact_properties dict must not bypass the
        JSON contract. Per ruling #5, decode failure in non-binary mode
        is always rejected / artifact_parse_error, regardless of what
        else the provider attached.
        """
        raw_bytes = b"\xff\xfe\x00garbage-not-json"
        # SLA uses a plain (non-binary) json_schema envelope.
        envelope = {
            "kind": "json_schema",
            "spec_version": "2020-12",
            "schema": {
                "type": "object",
                "required": ["summary"],
                "properties": {"summary": {"type": "string"}},
            },
        }
        sla = _make_sla_with_hash(usd, raw_bytes, envelope)
        # Provider tries to sneak validation through by providing properties.
        sneaky_props = {"summary": "I tricked you"}
        result, evidence = verifier.verify(
            sla, raw_bytes, artifact_properties=sneaky_props
        )
        assert result == "rejected"
        assert evidence["kind"] == "artifact_parse_error"


# ---------------------------------------------------------------------------
# Hash binding is step 1 (hash check before schema checks)
# ---------------------------------------------------------------------------
class TestHashBindingOrder:
    """Hash mismatch must be caught BEFORE any schema evaluation."""

    def test_hash_check_before_malformed_schema(
        self, verifier: SchemaVerifier, usd: AssetRef
    ) -> None:
        """Even with a malformed schema, hash mismatch returns hash_mismatch not sla_schema_malformed."""
        bad_envelope = {
            "kind": "json_schema",
            "spec_version": "2020-12",
            "schema": {"type": "not-a-real-type"},
        }
        artifact = _valid_artifact()
        sla = _make_sla_with_hash(usd, artifact, bad_envelope)
        tampered = b'{"summary": "tampered"}'
        # tampered != artifact, so hash mismatch fires before schema check
        result, evidence = verifier.verify(sla, tampered)
        assert result == "rejected"
        assert evidence["kind"] == "hash_mismatch"

    def test_hash_check_before_missing_schema(
        self, verifier: SchemaVerifier, usd: AssetRef
    ) -> None:
        """Even with empty schema, hash mismatch returns hash_mismatch."""
        artifact = _valid_artifact()
        sla = InterOrgSLA.create(**_base_sla_kwargs(usd, {}))
        artifact_hash = hashlib.sha256(artifact).hexdigest()
        sla = sla.with_delivery_hash(artifact_hash)
        # tampered bytes produce hash mismatch, not sla_missing_schema
        tampered = b'{"different": "content"}'
        result, evidence = verifier.verify(sla, tampered)
        assert result == "rejected"
        assert evidence["kind"] == "hash_mismatch"

    def test_empty_hash_returns_refunded_not_hash_mismatch(
        self, verifier: SchemaVerifier, usd: AssetRef
    ) -> None:
        """SLA with empty artifact_hash_at_delivery -> refunded, not rejected."""
        sla = InterOrgSLA.create(**_base_sla_kwargs(usd, _valid_schema_envelope()))
        # artifact_hash_at_delivery is empty ("") by default
        assert sla.artifact_hash_at_delivery == ""
        artifact = _valid_artifact()
        result, evidence = verifier.verify(sla, artifact)
        assert result == "refunded"
        assert evidence["kind"] == "sla_missing_schema"
        assert "artifact_hash_at_delivery" in evidence["detail"]


# ---------------------------------------------------------------------------
# Reserved kinds (v1b placeholders)
# ---------------------------------------------------------------------------
class TestReservedKinds:
    """executable_tests and composite are reserved; must return refunded."""

    def test_executable_tests_kind(self, verifier: SchemaVerifier, usd: AssetRef) -> None:
        envelope = {"kind": "executable_tests", "spec_version": "2020-12", "schema": {}}
        artifact = _valid_artifact()
        sla = _make_sla_with_hash(usd, artifact, envelope)
        result, evidence = verifier.verify(sla, artifact)
        assert result == "refunded"
        assert evidence["kind"] == "unsupported_schema_kind"

    def test_composite_kind(self, verifier: SchemaVerifier, usd: AssetRef) -> None:
        envelope = {"kind": "composite", "spec_version": "2020-12", "schema": {}}
        artifact = _valid_artifact()
        sla = _make_sla_with_hash(usd, artifact, envelope)
        result, evidence = verifier.verify(sla, artifact)
        assert result == "refunded"
        assert evidence["kind"] == "unsupported_schema_kind"


# ---------------------------------------------------------------------------
# Schema-envelope structural edge cases
# ---------------------------------------------------------------------------
class TestSchemaEnvelopeEdgeCases:
    """Structural variations in the deliverable_schema envelope."""

    def test_schema_key_missing_from_envelope(
        self, verifier: SchemaVerifier, usd: AssetRef
    ) -> None:
        """Envelope missing the 'schema' key -> refunded, sla_schema_malformed."""
        envelope = {"kind": "json_schema", "spec_version": "2020-12"}
        artifact = _valid_artifact()
        sla = _make_sla_with_hash(usd, artifact, envelope)
        result, evidence = verifier.verify(sla, artifact)
        assert result == "refunded"
        assert evidence["kind"] == "sla_schema_malformed"

    def test_none_spec_version(self, verifier: SchemaVerifier, usd: AssetRef) -> None:
        """spec_version: None -> refunded, unsupported_schema_version."""
        envelope = {"kind": "json_schema", "spec_version": None, "schema": {"type": "object"}}
        artifact = _valid_artifact()
        sla = _make_sla_with_hash(usd, artifact, envelope)
        result, evidence = verifier.verify(sla, artifact)
        assert result == "refunded"
        assert evidence["kind"] == "unsupported_schema_version"

    def test_none_kind(self, verifier: SchemaVerifier, usd: AssetRef) -> None:
        """kind: None -> refunded, unsupported_schema_kind."""
        envelope = {"kind": None, "spec_version": "2020-12", "schema": {"type": "object"}}
        artifact = _valid_artifact()
        sla = _make_sla_with_hash(usd, artifact, envelope)
        result, evidence = verifier.verify(sla, artifact)
        assert result == "refunded"
        assert evidence["kind"] == "unsupported_schema_kind"


# ---------------------------------------------------------------------------
# Evidence shape stability
# ---------------------------------------------------------------------------
class TestEvidenceShape:
    """All evidence dicts must have a 'kind' key (EvidenceKind discriminator)."""

    @pytest.mark.parametrize(
        "build_inputs",
        [
            "accepted",
            "rejected_schema_fail",
            "rejected_parse_error",
            "rejected_hash_mismatch",
            "refunded_missing_schema",
            "refunded_malformed_schema",
            "refunded_unsupported_kind",
            "refunded_unsupported_version",
        ],
    )
    def test_evidence_has_kind_key(
        self, verifier: SchemaVerifier, usd: AssetRef, build_inputs: str
    ) -> None:
        """Every outcome must include evidence['kind']."""
        result, evidence = _build_case(verifier, usd, build_inputs)
        assert "kind" in evidence, f"evidence missing 'kind' for case {build_inputs!r}"


def _build_case(
    verifier: SchemaVerifier, usd: AssetRef, case: str
) -> tuple[Any, dict]:
    """Build a (result, evidence) pair for each named case."""
    artifact = _valid_artifact()

    if case == "accepted":
        sla = _make_sla_with_hash(usd, artifact)
        return verifier.verify(sla, artifact)

    if case == "rejected_schema_fail":
        bad_artifact = json.dumps({"score": 1}).encode()
        sla = _make_sla_with_hash(usd, bad_artifact)
        return verifier.verify(sla, bad_artifact)

    if case == "rejected_parse_error":
        raw = b"\x00\x01\x02"
        sla = _make_sla_with_hash(usd, raw)
        return verifier.verify(sla, raw)

    if case == "rejected_hash_mismatch":
        sla = _make_sla_with_hash(usd, artifact)
        return verifier.verify(sla, b'{"summary": "different"}')

    if case == "refunded_missing_schema":
        sla = InterOrgSLA.create(**_base_sla_kwargs(usd, {}))
        sla = sla.with_delivery_hash(hashlib.sha256(artifact).hexdigest())
        return verifier.verify(sla, artifact)

    if case == "refunded_malformed_schema":
        env = {"kind": "json_schema", "spec_version": "2020-12", "schema": {"type": "not-a-real-type"}}
        sla = _make_sla_with_hash(usd, artifact, env)
        return verifier.verify(sla, artifact)

    if case == "refunded_unsupported_kind":
        env = {"kind": "future_kind", "spec_version": "2020-12", "schema": {}}
        sla = _make_sla_with_hash(usd, artifact, env)
        return verifier.verify(sla, artifact)

    if case == "refunded_unsupported_version":
        env = {"kind": "json_schema", "spec_version": "draft-07", "schema": {}}
        sla = _make_sla_with_hash(usd, artifact, env)
        return verifier.verify(sla, artifact)

    raise ValueError(f"unknown case: {case!r}")
