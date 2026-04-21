"""
core/primitives/schema_verifier.py -- Tier 0 deterministic schema verifier
===========================================================================
Ticket A1 of the v1a Oracle build.

`SchemaVerifier` is the Tier 0 evaluation engine. It verifies a delivered
artifact against the `deliverable_schema` embedded in an `InterOrgSLA`,
returning a `(OracleResult, evidence_dict)` tuple. No side effects, no
network, no LLM, no clock.

v1a scope
---------
Only `kind: "json_schema"` schemas are supported. Kinds `"executable_tests"`
and `"composite"` are reserved for v1b and return `refunded` immediately.
Only `spec_version: "2020-12"` is accepted; other values return `refunded`.

`accuracy_requirement` in v1a
------------------------------
The `accuracy_requirement` field on InterOrgSLA is present and canonical
in v1a but is intentionally IGNORED here. Tier 0 does not produce a rubric
score; `OracleVerdict.score` stays `None` for all Tier 0 verdicts. The field
is consumed by Tier 1 (v1b). SLA drafters should not expect probabilistic
scoring from Tier 0 -- see ORACLE.md for the full tier breakdown.

Binary artifacts
----------------
When `deliverable_schema.artifact_format == "binary"`, the schema validates
`artifact_properties` (a caller-supplied dict) rather than the raw bytes.
The caller (e.g. the provider at delivery time) is responsible for populating
`artifact_properties`. If it is absent when required, the result is
`refunded` with `evidence.kind = "sla_missing_schema"`.

Hash binding is ALWAYS step 1
------------------------------
`sha256(artifact_bytes)` is compared to `sla.artifact_hash_at_delivery`
before any schema logic. A mismatch returns `rejected` immediately. If
`artifact_hash_at_delivery` is empty (not yet populated via
`sla.with_delivery_hash`), the result is `refunded` with
`evidence.kind = "sla_missing_schema"`.

Import hygiene
--------------
This module does NOT import `OracleVerdict`. It imports only
`OracleResult` and `EvidenceKind` from `core.primitives.oracle`, which are
cheap Literal types. `OracleVerdict` construction is the responsibility of
the caller (e.g. Oracle.evaluate_tier0 in Ticket A4).
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from jsonschema import Draft202012Validator, SchemaError, ValidationError

from core.primitives.oracle import EvidenceKind, OracleResult
from core.primitives.sla import InterOrgSLA

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_SUPPORTED_SCHEMA_KIND = "json_schema"
_SUPPORTED_SPEC_VERSION = "2020-12"

# Kept as a module-level constant to make the ruling explicit and searchable.
_RESERVED_KINDS = frozenset({"executable_tests", "composite"})


# ---------------------------------------------------------------------------
# SchemaVerifier
# ---------------------------------------------------------------------------
class SchemaVerifier:
    """Tier 0 deterministic schema verifier.

    Stateless class: no __init__ args, no instance state, all methods pure.
    Construct once and reuse freely; or call the class method directly.

    The only public entry point is `verify`. Every ruling from the A1 spec
    is implemented here. See the module docstring for the full ordering.
    """

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def verify(
        self,
        sla: InterOrgSLA,
        artifact_bytes: bytes,
        *,
        artifact_properties: dict | None = None,
    ) -> tuple[OracleResult, dict]:
        """Verify a delivered artifact against the SLA's deliverable_schema.

        Parameters
        ----------
        sla:
            The InterOrgSLA that governs the delivery. Must have
            `artifact_hash_at_delivery` populated (non-empty) before
            calling this method.
        artifact_bytes:
            The raw bytes of the delivered artifact.
        artifact_properties:
            Optional dict populated by the provider at delivery time.
            Required when `deliverable_schema.artifact_format == "binary"`.
            Ignored when the artifact is JSON-decodable and no binary
            hint is set.

        Returns
        -------
        tuple[OracleResult, dict]
            A (result, evidence) pair where result is one of
            "accepted" | "rejected" | "refunded" and evidence is a dict
            with at minimum a "kind" key drawn from EvidenceKind.

        Ruling order (non-negotiable per spec)
        ----------------------------------------
        1. Hash binding -- must match before any schema logic.
        2. Missing deliverable_schema on SLA.
        3. Schema kind check -- only "json_schema" in v1a.
        4. Spec version check -- only "2020-12" in v1a.
        5. Schema structure validation (SchemaError).
        6. Artifact payload resolution (JSON decode or artifact_properties).
        7. JSON Schema validation of the payload.
        """
        # Step 1: artifact hash binding
        hash_result = self._check_hash(sla, artifact_bytes)
        if hash_result is not None:
            return hash_result

        # Step 2: missing deliverable_schema
        schema_envelope = sla.deliverable_schema
        if not schema_envelope:
            return ("refunded", {"kind": "sla_missing_schema"})

        # Step 3: schema kind
        kind = schema_envelope.get("kind")
        if kind in _RESERVED_KINDS:
            return (
                "refunded",
                {
                    "kind": "unsupported_schema_kind",
                    "detail": f"kind {kind!r} is reserved for v1b",
                },
            )
        if kind != _SUPPORTED_SCHEMA_KIND:
            return (
                "refunded",
                {
                    "kind": "unsupported_schema_kind",
                    "detail": f"unknown kind {kind!r}; only 'json_schema' supported in v1a",
                },
            )

        # Step 4: spec_version
        spec_version = schema_envelope.get("spec_version")
        if spec_version != _SUPPORTED_SPEC_VERSION:
            return (
                "refunded",
                {
                    "kind": "unsupported_schema_version",
                    "detail": (
                        f"spec_version {spec_version!r} is not supported; "
                        f"only '2020-12' is accepted in v1a"
                    ),
                },
            )

        # Step 5: schema structure validation
        raw_schema = schema_envelope.get("schema")
        if raw_schema is None:
            return (
                "refunded",
                {
                    "kind": "sla_schema_malformed",
                    "detail": "deliverable_schema missing 'schema' key",
                },
            )
        try:
            validator = Draft202012Validator(raw_schema)
            # check_schema raises SchemaError if the schema itself is invalid
            Draft202012Validator.check_schema(raw_schema)
        except SchemaError as exc:
            return (
                "refunded",
                {
                    "kind": "sla_schema_malformed",
                    "detail": str(exc.message),
                },
            )

        # Step 6: artifact payload resolution
        # If the SLA declares artifact_format="binary", the provider MUST
        # supply artifact_properties (the dict the schema validates against).
        # Otherwise JSON is expected: decode the bytes or reject. No fuzzy
        # fallback -- an unlabeled artifact with stray properties is the
        # SLA drafter's problem; strict JSON expectation per ruling #5.
        is_binary = schema_envelope.get("artifact_format") == "binary"
        if is_binary:
            if artifact_properties is None:
                return (
                    "refunded",
                    {
                        "kind": "sla_missing_schema",
                        "detail": "binary artifact requires artifact_properties",
                    },
                )
            payload: Any = artifact_properties
        else:
            try:
                payload = json.loads(artifact_bytes.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                return (
                    "rejected",
                    {
                        "kind": "artifact_parse_error",
                        "detail": str(exc),
                    },
                )

        # Step 7: schema validation
        errors = list(validator.iter_errors(payload))
        if errors:
            # Return the first (most specific) error for determinism.
            first = errors[0]
            return (
                "rejected",
                {
                    "kind": "schema_fail",
                    "detail": first.message,
                    "path": list(first.absolute_path),
                },
            )

        return ("accepted", {"kind": "schema_pass"})

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------
    def _check_hash(
        self,
        sla: InterOrgSLA,
        artifact_bytes: bytes,
    ) -> tuple[OracleResult, dict] | None:
        """Check artifact hash binding.

        Returns a (result, evidence) tuple if binding fails or is
        not yet populated, otherwise returns None (binding OK, proceed).

        Ruling: if artifact_hash_at_delivery is empty, the SLA was not
        prepared for delivery -- return refunded. If it is non-empty,
        compare sha256(artifact_bytes) against it.
        """
        expected = sla.artifact_hash_at_delivery
        if not expected:
            return (
                "refunded",
                {
                    "kind": "sla_missing_schema",
                    "detail": "artifact_hash_at_delivery not populated",
                },
            )
        actual = hashlib.sha256(artifact_bytes).hexdigest()
        if actual != expected:
            return (
                "rejected",
                {
                    "kind": "hash_mismatch",
                    "expected": expected,
                    "actual": actual,
                },
            )
        return None


__all__ = ["SchemaVerifier"]
