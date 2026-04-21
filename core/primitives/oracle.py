"""
core/primitives/oracle.py -- OracleVerdict primitive (v1a wire shape)
======================================================================
Ticket A2 of the v1a Oracle build. This module defines the
`OracleVerdict` frozen dataclass and its canonical serialization helpers.

V1a scope
---------
Two evaluation tiers are live in v1a:
  - Tier 0: deterministic JSON Schema validation (SchemaVerifier, Ticket A1).
  - Tier 3: founder arbitration override (Oracle class, Ticket A4).

`OracleVerdict` is the wire-shape contract for BOTH tiers. A4's
`Oracle.evaluate_tier0` wraps Tier-0 `(result, evidence)` tuples into
signed `OracleVerdict` instances. A5's `release_pending_verdict` adapter
method consumes these verdicts at the settlement boundary.

Tier 1 (probabilistic scoring) is deferred to v1b. The `score` field is
reserved and canonical now so the shape is stable when Tier 1 ships.

Canonical serialization rules (supplement to sla.py rules)
-----------------------------------------------------------
`_canonical_bytes` produces the byte input for both `verdict_hash`
and the Ed25519 signature. Rules that extend the SLA idiom:

1. `sort_keys=True`, `separators=(",",":")`, `ensure_ascii=False`.
2. `Decimal` -> str via `f"{d:f}"` (fixed notation, no scientific).
3. `signer` serialized as `{"bytes_hex": "..."}`, not raw string.
4. `signature` field EXCLUDED from canonical bytes (same pattern as SLA).
5. `verdict_hash` itself EXCLUDED during its own computation (chicken-and-egg
   same as `integrity_binding` on SLA).
6. `evidence` dict is recursively key-sorted via `sort_keys=True` in
   json.dumps -- no manual recursion needed.
7. `score: None` emits as `"score": null` (never omitted) so the field
   shape is stable across scored and unscored verdicts.

`OracleVerdict.create` factory
-------------------------------
The strict entry point: validates all inputs, computes canonical bytes,
derives `verdict_hash`, signs canonical bytes, and constructs the
frozen instance. The raw constructor is available for `from_dict`
rehydration; callers should prefer `create`.

`from_dict` / `to_dict`
-----------------------
Full round-trip. `from_dict` rejects unknown `evidence.kind` with
`VerdictError`. Signatures round-trip via `Signature.to_dict` /
`Signature.from_dict`.
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal, get_args

if TYPE_CHECKING:
    from core.primitives.node_registry import NodeRegistry

from core.primitives.canonicalizer_registry import (
    default_canonicalizer_registry,
    extract_protocol_version,
)
from core.primitives.exceptions import (
    EvaluatorAuthorizationError,
    SignatureError,
    VerdictError,
)
from core.primitives.identity import (
    Ed25519Keypair,
    Ed25519PublicKey,
    Signature,
    sign as _identity_sign,
    verify as _identity_verify,
)
from core.primitives.signer import LocalKeypairSigner, Signer
from core.primitives.sla import InterOrgSLA
from core.primitives.state import FOUNDER_PRINCIPALS

# ---------------------------------------------------------------------------
# Literal types
# ---------------------------------------------------------------------------
OracleTier = Literal[0, 1, 2, 3]
OracleResult = Literal["accepted", "rejected", "refunded"]

EvidenceKind = Literal[
    "schema_pass",
    "schema_fail",
    "hash_mismatch",
    "artifact_parse_error",
    "sla_schema_malformed",
    "sla_missing_schema",
    "unsupported_schema_kind",
    "unsupported_schema_version",
    "founder_override",
    # v1b additions -- used by Tier 1 probabilistic evaluators.
    # evaluator_error: the evaluator raised an exception during scoring.
    # evaluator_timeout: the evaluator did not respond within the deadline.
    # schema_pass_with_score: Tier 1 passed schema AND produced a numeric score.
    # _VALID_EVIDENCE_KINDS (below) is semi-public: evaluator.py imports it
    # to validate EvaluationOutput at construction time.
    "evaluator_error",
    "evaluator_timeout",
    "schema_pass_with_score",
]

# Resolved at module load so `from_dict` can validate without importing
# typing internals at call time.
_VALID_EVIDENCE_KINDS: frozenset[str] = frozenset(get_args(EvidenceKind))
_VALID_TIERS: frozenset[int] = frozenset(get_args(OracleTier))
_VALID_RESULTS: frozenset[str] = frozenset(get_args(OracleResult))

_PROTOCOL_VERSION_DEFAULT = "companyos-verdict/0.1"

# Fields ALWAYS excluded from canonical bytes.
# `signature` is excluded to avoid the chicken-and-egg problem (same pattern
# as SLA signature fields). `verdict_hash` exclusion is conditional and
# controlled by the `exclude_verdict_hash` flag in `_verdict_shell_dict`.
_ALWAYS_EXCLUDED_FROM_CANONICAL = frozenset({"signature"})


# ---------------------------------------------------------------------------
# Private serialization helpers
# ---------------------------------------------------------------------------
def _json_default(obj: Any) -> Any:
    """JSON fallback for non-standard types.

    `Decimal` -> fixed-notation string. All other non-primitive types must be
    converted to dict/str/int/float/bool/None before reaching json.dumps.
    """
    if isinstance(obj, Decimal):
        return f"{obj:f}"
    raise TypeError(
        f"_canonical_bytes cannot serialize {type(obj).__name__}: "
        f"convert to primitive before canonicalization"
    )


def _verdict_shell_dict(
    verdict: "OracleVerdict | dict[str, Any]",
    *,
    exclude_verdict_hash: bool = False,
) -> dict[str, Any]:
    """Build the dict that feeds into `_canonical_bytes`.

    Excluded fields:
      - `signature` (always, for signing-body invariance).
      - `verdict_hash` (only when `exclude_verdict_hash=True`, used during
        initial hash computation to avoid chicken-and-egg).

    `signer` is serialized as `{"bytes_hex": "..."}` so the canonical bytes
    include a stable dict representation rather than whatever str() would
    produce.
    """
    if isinstance(verdict, OracleVerdict):
        raw: dict[str, Any] = {
            "sla_id": verdict.sla_id,
            "artifact_hash": verdict.artifact_hash,
            "tier": verdict.tier,
            "result": verdict.result,
            "evaluator_did": verdict.evaluator_did,
            "evidence": verdict.evidence,
            "verdict_hash": verdict.verdict_hash,
            "signer": verdict.signer,
            "signature": verdict.signature,
            "issued_at": verdict.issued_at,
            "protocol_version": verdict.protocol_version,
            "score": verdict.score,
        }
    else:
        raw = dict(verdict)

    out: dict[str, Any] = {}
    for key, value in raw.items():
        if key in _ALWAYS_EXCLUDED_FROM_CANONICAL:
            continue
        if exclude_verdict_hash and key == "verdict_hash":
            continue
        if isinstance(value, Ed25519PublicKey):
            out[key] = value.to_dict()
        elif isinstance(value, Decimal):
            # Score: emit as "score": null when None is handled outside;
            # non-None Decimal goes through _json_default via json.dumps.
            out[key] = value
        else:
            out[key] = value
    return out


def _canonical_bytes(
    verdict: "OracleVerdict | dict[str, Any]",
    *,
    exclude_verdict_hash: bool = False,
) -> bytes:
    """Produce canonical UTF-8 JSON bytes for a verdict.

    `signature` is always excluded. `verdict_hash` excluded when
    `exclude_verdict_hash=True` (used when computing the hash itself).

    `sort_keys=True` handles recursive key-sorting of nested dicts
    (including `evidence`), matching the SLA canonical idiom.

    This function encodes the companyos-verdict/0.1 canonicalization rules.
    It is registered into `default_canonicalizer_registry` below so that
    both sign-time (OracleVerdict.create) and verify-time
    (OracleVerdict.verify_signature) dispatch through the registry.
    """
    shell = _verdict_shell_dict(verdict, exclude_verdict_hash=exclude_verdict_hash)
    return json.dumps(
        shell,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=_json_default,
    ).encode("utf-8")


# Register the v1a canonicalization rules.
# This runs at module-load time. oracle.py imports the registry (not the
# reverse), so there is no circular import. Future protocol versions are
# registered by importing this module and calling
# default_canonicalizer_registry.register("companyos-verdict/0.x", fn).
default_canonicalizer_registry.register(
    "companyos-verdict/0.1",
    _canonical_bytes,
)

# Register v1b slot: companyos-verdict/0.2.
# The 0.2 byte rules are IDENTICAL to 0.1 for now. The version bump exists
# to prove the registry dispatches correctly and to reserve the slot for
# real 0.2 canonicalization changes in a future ticket. Tier 1 verdicts
# (built in B2) will pass protocol_version="companyos-verdict/0.2"
# explicitly when calling OracleVerdict.create. The default (_PROTOCOL_VERSION_DEFAULT)
# stays "companyos-verdict/0.1" so existing B0 / v1a code paths are unchanged.
default_canonicalizer_registry.register(
    "companyos-verdict/0.2",
    _canonical_bytes,  # v0.2 byte rules are identical to v0.1 for now
)


# ---------------------------------------------------------------------------
# OracleVerdict
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class OracleVerdict:
    """Immutable signed verdict from an oracle node over a delivered artifact.

    Field ordering note: Python frozen dataclasses require default-less fields
    first. Semantic ordering is preserved in `to_dict` output via sort_keys;
    the Python field order is driven by the default-vs-required split.

    Construction: prefer `OracleVerdict.create(...)`. The raw constructor is
    available for `from_dict` rehydration but skips all validation.
    """

    # --- required fields ----------------------------------------------------
    sla_id: str
    artifact_hash: str
    tier: OracleTier
    result: OracleResult
    evaluator_did: str
    evidence: dict
    verdict_hash: str
    signer: Ed25519PublicKey
    signature: Signature
    issued_at: str

    # --- defaulted fields ---------------------------------------------------
    protocol_version: str = _PROTOCOL_VERSION_DEFAULT
    score: "Decimal | None" = None

    # -----------------------------------------------------------------------
    # Factory
    # -----------------------------------------------------------------------
    @classmethod
    def create(
        cls,
        *,
        sla_id: str,
        artifact_hash: str,
        tier: OracleTier,
        result: OracleResult,
        evaluator_did: str,
        evidence: dict,
        issued_at: str,
        signer: "Signer",
        protocol_version: str = _PROTOCOL_VERSION_DEFAULT,
        score: "Decimal | None" = None,
    ) -> "OracleVerdict":
        """Strict factory: validate, hash, sign, and return a frozen verdict.

        `signer` provides both the signature and the embedded `signer`
        public key via its `.public_key` property and `.sign()` method.
        There is exactly one signer per verdict (unlike the SLA where
        requester + provider both sign independently).

        `verdict_hash` is the sha256 hex digest of the canonical bytes with
        BOTH `signature` and `verdict_hash` excluded. Sign the same canonical
        bytes so the signature commits to every field except itself.

        Raises:
            ValueError: on invalid field values.
            TypeError: on wrong argument types.
            VerdictError: if `evidence["kind"]` is an unknown EvidenceKind.
        """
        # --- scalar validation ----------------------------------------------
        if not isinstance(sla_id, str) or not sla_id:
            raise ValueError("sla_id must be a non-empty string")
        if not isinstance(artifact_hash, str) or not artifact_hash:
            raise ValueError("artifact_hash must be a non-empty string")
        if tier not in _VALID_TIERS:
            raise ValueError(
                f"tier must be one of {sorted(_VALID_TIERS)}, got {tier!r}"
            )
        if result not in _VALID_RESULTS:
            raise ValueError(
                f"result must be one of {sorted(_VALID_RESULTS)}, got {result!r}"
            )
        if not isinstance(evaluator_did, str) or not evaluator_did:
            raise ValueError("evaluator_did must be a non-empty string")
        if not isinstance(evidence, dict):
            raise TypeError(
                f"evidence must be a dict, got {type(evidence).__name__}"
            )
        if not isinstance(issued_at, str) or not issued_at:
            raise ValueError("issued_at must be a non-empty string")
        if not isinstance(signer, Signer):
            raise TypeError(
                f"signer must be a Signer, got {type(signer).__name__}"
            )
        if score is not None and not isinstance(score, Decimal):
            raise TypeError(
                f"score must be Decimal or None, got {type(score).__name__}"
            )

        # --- evidence kind check --------------------------------------------
        kind = evidence.get("kind")
        if kind not in _VALID_EVIDENCE_KINDS:
            raise VerdictError(f"unknown evidence kind: {kind!r}")

        # --- compute canonical bytes and hash -------------------------------
        # Build a shell dict that stands in for the not-yet-constructed
        # OracleVerdict (same technique as InterOrgSLA.create).
        shell: dict[str, Any] = {
            "sla_id": sla_id,
            "artifact_hash": artifact_hash,
            "tier": tier,
            "result": result,
            "evaluator_did": evaluator_did,
            "evidence": evidence,
            # verdict_hash placeholder excluded below
            "signer": signer.public_key,
            # signature excluded by canonicalizer
            "issued_at": issued_at,
            "protocol_version": protocol_version,
            "score": score,
        }

        # Protocol-constant step (ruling 20): read protocol_version from the
        # shell BEFORE invoking any canonicalizer. The version parse is
        # protocol-constant; the canonicalizer is protocol-varying.
        _version = extract_protocol_version(shell)
        canonicalize = default_canonicalizer_registry.get(_version)

        body = canonicalize(shell, exclude_verdict_hash=True)
        verdict_hash = hashlib.sha256(body).hexdigest()

        # Include verdict_hash in the bytes that get signed so the signature
        # commits to the hash (and therefore to all content fields).
        shell_with_hash = dict(shell, verdict_hash=verdict_hash)
        signing_body = canonicalize(shell_with_hash, exclude_verdict_hash=False)
        sig = signer.sign(signing_body)

        return cls(
            sla_id=sla_id,
            artifact_hash=artifact_hash,
            tier=tier,
            result=result,
            evaluator_did=evaluator_did,
            evidence=evidence,
            verdict_hash=verdict_hash,
            signer=signer.public_key,
            signature=sig,
            issued_at=issued_at,
            protocol_version=protocol_version,
            score=score,
        )

    # -----------------------------------------------------------------------
    # Signature verification
    # -----------------------------------------------------------------------
    def verify_signature(
        self,
        registry: "NodeRegistry | None" = None,
    ) -> None:
        """Verify the embedded Ed25519 signature over canonical bytes.

        Recomputes canonical bytes (signature excluded, verdict_hash
        included) and checks the signature against `self.signer`.

        Parameters
        ----------
        registry:
            Optional `NodeRegistry`. When `None` (the default), the method
            performs only the v1a checks: signer-consistency and
            cryptographic signature verification. This preserves backward
            compatibility for all existing callers.

            When a registry is provided, a third check is added AFTER the
            cryptographic verify succeeds:
              1. Resolve `self.evaluator_did` via `registry.get(did)`.
                 If the DID is not registered, raises
                 `SignatureError("unknown evaluator DID: <did>")`.
              2. Compare the registered pubkey against `self.signer`.
                 If they differ, raises
                 `SignatureError("evaluator pubkey does not match
                 registered pubkey for <did>")`.

            The registry check runs LAST so that a tampered verdict
            (whose signature fails the cryptographic check) surfaces as
            "signature failed verification", not "unknown DID", even when
            both failures would apply.

        Raises:
            SignatureError: if the signer embedded in `self.signature`
                does not match `self.signer`, if cryptographic
                verification fails (tampered fields, wrong keypair, etc.),
                or (when registry is provided) if the evaluator_did is
                unknown or its registered pubkey does not match
                `self.signer`.

        v1b gap status:

        - NodeRegistry gap: CLOSED when registry is passed. Callers that
          supply a NodeRegistry get evaluator_did -> pubkey binding
          verification. Callers that pass no registry retain v1a behavior.
        - Protocol-version forward-compat: OPEN (deferred to v1c).
          If v1c changes canonicalization rules, historical v1a verdicts
          (protocol_version == "companyos-verdict/0.1") will fail
          verification under the new rules. v1c should dispatch byte
          derivation through a version-keyed canonicalizer registry so
          archived verdicts remain auditable.
        """
        # Step 1: Signer consistency. The pubkey in the Signature must equal
        # the top-level `signer` field. This prevents signature/signer drift
        # where someone swaps one field without the other.
        if self.signature.signer != self.signer:
            raise SignatureError(
                "signature.signer does not match top-level signer field"
            )

        # Step 2: Cryptographic verify. Protocol-constant step (ruling 20):
        # read protocol_version from the verdict BEFORE invoking any
        # canonicalizer. The version parse is protocol-constant; the
        # canonicalizer is protocol-varying. Unknown protocol_version raises
        # ValueError (not SignatureError).
        _version = self.protocol_version
        canonicalize = default_canonicalizer_registry.get(_version)

        # Recompute the bytes that were signed in `create`: canonical bytes
        # with verdict_hash included and signature excluded.
        signing_body = canonicalize(self, exclude_verdict_hash=False)
        if not _identity_verify(self.signature, signing_body):
            raise SignatureError(
                "OracleVerdict signature failed cryptographic verification"
            )

        # Step 3 (registry mode only): DID -> pubkey binding check.
        # Runs after crypto verify so tamper failures surface before DID errors.
        if registry is not None:
            try:
                registered_pubkey = registry.get(self.evaluator_did)
            except KeyError:
                raise SignatureError(
                    f"unknown evaluator DID: {self.evaluator_did}"
                )
            if registered_pubkey != self.signer:
                raise SignatureError(
                    f"evaluator pubkey does not match registered pubkey "
                    f"for {self.evaluator_did}"
                )

    # -----------------------------------------------------------------------
    # Serialization
    # -----------------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        """Emit a fully-serializable dict including all fields.

        `score` is always present (either a string for a non-None Decimal or
        `None` as JSON null) so the shape is stable.
        """
        return {
            "sla_id": self.sla_id,
            "artifact_hash": self.artifact_hash,
            "tier": self.tier,
            "result": self.result,
            "evaluator_did": self.evaluator_did,
            "evidence": self.evidence,
            "verdict_hash": self.verdict_hash,
            "signer": self.signer.to_dict(),
            "signature": self.signature.to_dict(),
            "issued_at": self.issued_at,
            "protocol_version": self.protocol_version,
            "score": f"{self.score:f}" if self.score is not None else None,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "OracleVerdict":
        """Rehydrate from a `to_dict` payload.

        Validates `evidence.kind` and rejects unknown values with
        `VerdictError`. Does NOT re-verify the signature or recompute
        `verdict_hash` -- callers must call `verify_signature()` explicitly
        if they need cryptographic assurance after load.

        Raises:
            ValueError: on missing required fields.
            VerdictError: if `evidence["kind"]` is unknown.
        """
        required = (
            "sla_id",
            "artifact_hash",
            "tier",
            "result",
            "evaluator_did",
            "evidence",
            "verdict_hash",
            "signer",
            "signature",
            "issued_at",
        )
        for key in required:
            if key not in d:
                raise ValueError(f"OracleVerdict.from_dict missing field {key!r}")

        evidence = dict(d["evidence"])
        kind = evidence.get("kind")
        if kind not in _VALID_EVIDENCE_KINDS:
            raise VerdictError(f"unknown evidence kind: {kind!r}")

        score_raw = d.get("score")
        score: "Decimal | None" = Decimal(str(score_raw)) if score_raw is not None else None

        return cls(
            sla_id=str(d["sla_id"]),
            artifact_hash=str(d["artifact_hash"]),
            tier=int(d["tier"]),  # type: ignore[arg-type]
            result=str(d["result"]),  # type: ignore[arg-type]
            evaluator_did=str(d["evaluator_did"]),
            evidence=evidence,
            verdict_hash=str(d["verdict_hash"]),
            signer=Ed25519PublicKey.from_dict(d["signer"]),
            signature=Signature.from_dict(d["signature"]),
            issued_at=str(d["issued_at"]),
            protocol_version=str(
                d.get("protocol_version", _PROTOCOL_VERSION_DEFAULT)
            ),
            score=score,
        )


__all__ = [
    "OracleTier",
    "OracleResult",
    "EvidenceKind",
    "OracleVerdict",
    "Oracle",
]


# ---------------------------------------------------------------------------
# Private helpers (used by Oracle below)
# ---------------------------------------------------------------------------
def _datetime_now_utc_z() -> str:
    """Return the current UTC time as a canonical Z-suffix ISO string."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Oracle
# ---------------------------------------------------------------------------
class Oracle:
    """Evaluation node that issues signed OracleVerdicts.

    v1a supports two tiers:
      - Tier 0: deterministic JSON Schema validation via SchemaVerifier.
      - Tier 3: founder arbitration override (supersedes any prior verdict).

    Attributes
    ----------
    node_did:
        The DID of the node on which this Oracle instance is running.
        Stamped into `evaluator_did` on every verdict produced here.
    node_keypair:
        The Ed25519 keypair of this node. Used to sign Tier 0 verdicts.
        Tier 3 verdicts are signed with the founder's keypair instead,
        but `node_did` is still recorded as `evaluator_did`.
    schema_verifier:
        A SchemaVerifier instance (or any object with a compatible
        `verify(sla, artifact_bytes, *, artifact_properties)` method).
    """

    def __init__(
        self,
        node_did: str,
        node_keypair: Ed25519Keypair,
        schema_verifier: "SchemaVerifier",
        evaluator_timeout_sec: int = 30,
    ) -> None:
        if not isinstance(node_did, str) or not node_did.strip():
            raise ValueError("node_did must be a non-empty string")
        if not isinstance(node_keypair, Ed25519Keypair):
            raise TypeError(
                f"node_keypair must be an Ed25519Keypair, "
                f"got {type(node_keypair).__name__}"
            )
        if not isinstance(evaluator_timeout_sec, int) or evaluator_timeout_sec <= 0:
            raise ValueError("evaluator_timeout_sec must be a positive int")
        self.node_did = node_did
        self.node_keypair = node_keypair
        self.schema_verifier = schema_verifier
        self.evaluator_timeout_sec = evaluator_timeout_sec

    def evaluate_tier0(
        self,
        sla: "InterOrgSLA",
        artifact_bytes: bytes,
        *,
        artifact_properties: dict | None = None,
    ) -> OracleVerdict:
        """Run Tier 0 (schema) evaluation and return a signed verdict.

        Delegates to `self.schema_verifier.verify`, wraps the returned
        `(result, evidence)` tuple in an `OracleVerdict` with `tier=0`,
        and signs it with `self.node_keypair`.

        Parameters
        ----------
        sla:
            The InterOrgSLA governing this delivery. Its
            `artifact_hash_at_delivery` must already be populated via
            `sla.with_delivery_hash(...)`.
        artifact_bytes:
            Raw bytes of the delivered artifact.
        artifact_properties:
            Optional dict for binary artifacts. Passed through to
            `SchemaVerifier.verify` unchanged.

        Returns
        -------
        OracleVerdict
            A signed Tier 0 verdict. `evaluator_did` is `self.node_did`.
        """
        result, evidence = self.schema_verifier.verify(
            sla, artifact_bytes, artifact_properties=artifact_properties
        )
        artifact_hash = hashlib.sha256(artifact_bytes).hexdigest()
        issued_at = _datetime_now_utc_z()
        return OracleVerdict.create(
            sla_id=sla.sla_id,
            artifact_hash=artifact_hash,
            tier=0,
            result=result,
            evaluator_did=self.node_did,
            evidence=evidence,
            issued_at=issued_at,
            signer=LocalKeypairSigner(self.node_keypair),
        )

    def founder_override(
        self,
        prior_verdict: OracleVerdict,
        result: OracleResult,
        reason: str,
        founder_signer: "Signer",
        *,
        founder_identity: str,
    ) -> OracleVerdict:
        """Issue a Tier 3 founder-arbitration verdict that supersedes a prior verdict.

        The prior verdict's `artifact_hash` and `sla_id` are carried forward.
        The verdict is signed by `founder_signer`, not `self.node_keypair`,
        so the cryptographic signer is the founder. `evaluator_did` is still
        `self.node_did` (the node processing the override).

        Parameters
        ----------
        prior_verdict:
            The verdict being overridden. Its `verdict_hash` is recorded
            in `evidence.overrides` for auditability.
        result:
            The new result to assert (e.g. "accepted" to reverse a rejection).
        reason:
            A non-empty human-readable rationale for the override.
        founder_signer:
            A `Signer` instance (e.g. `LocalKeypairSigner(keypair)`) used to
            sign the verdict. Passing a raw `Ed25519Keypair` raises a
            `TypeError` with a clear migration message.
        founder_identity:
            A string identity claim checked against `FOUNDER_PRINCIPALS`.
            Raises `SignatureError` if not present in that set.

        Returns
        -------
        OracleVerdict
            A signed Tier 3 verdict with `evidence.kind="founder_override"`.

        Raises
        ------
        TypeError
            If a raw `Ed25519Keypair` is passed as `founder_signer` (clean
            break -- wrap it in `LocalKeypairSigner(keypair)` instead).
        ValueError
            If `reason` is empty or whitespace-only.
        SignatureError
            If `founder_identity` is not in FOUNDER_PRINCIPALS.
        """
        # Guard against callers passing a raw keypair (pre-v1b call shape).
        if isinstance(founder_signer, Ed25519Keypair):
            raise TypeError(
                "Oracle.founder_override now requires a Signer. "
                "Wrap your Ed25519Keypair in LocalKeypairSigner(keypair)."
            )
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reason must be a non-empty string")
        if founder_identity not in FOUNDER_PRINCIPALS:
            raise SignatureError(
                f"non-founder identity: {founder_identity!r}"
            )
        issued_at = _datetime_now_utc_z()
        evidence = {
            "kind": "founder_override",
            "overrides": prior_verdict.verdict_hash,
            "reason": reason,
            "founder_identity": founder_identity,
        }
        return OracleVerdict.create(
            sla_id=prior_verdict.sla_id,
            artifact_hash=prior_verdict.artifact_hash,
            tier=3,
            result=result,
            evaluator_did=self.node_did,
            evidence=evidence,
            issued_at=issued_at,
            signer=founder_signer,
        )

    def evaluate_tier1(
        self,
        sla: "InterOrgSLA",
        artifact_bytes: bytes,
        *,
        evaluator: "PrimaryEvaluator",
        artifact_properties: dict | None = None,
    ) -> OracleVerdict:
        """Run Tier 1 (probabilistic) evaluation and return a signed verdict.

        Authorization checks (run before calling the evaluator):
          1. Canonical hash check: if `sla.canonical_evaluator_hash` is set,
             it MUST equal `evaluator.canonical_hash`; else raises
             `EvaluatorAuthorizationError`.
          2. Counterparty check: `evaluator.evaluator_did` must not equal
             `sla.requester_node_did` or `sla.provider_node_did`; else raises
             `EvaluatorAuthorizationError`.

        Mechanical gate: delegates to `SchemaVerifier.verify`. If the schema
        result is not "accepted", returns a Tier 0 verdict immediately (the
        evaluator is NOT called). The returned verdict's evidence carries
        `tier1_skipped_via_mechanical_fail: true`.

        Evaluator call: wrapped in a thread-based wall-clock timeout
        (`self.evaluator_timeout_sec`). On timeout, returns a refunded verdict
        with `evidence.kind = "evaluator_timeout"`.

        Parameters
        ----------
        sla:
            The InterOrgSLA governing this delivery.
        artifact_bytes:
            Raw bytes of the delivered artifact.
        evaluator:
            A `PrimaryEvaluator` instance to call after the mechanical gate.
        artifact_properties:
            Optional dict for binary artifacts. Passed to both the schema
            verifier and the evaluator unchanged.

        Returns
        -------
        OracleVerdict
            A signed Tier 1 verdict. `evaluator_did` is set to
            `evaluator.evaluator_did`.

        Raises
        ------
        EvaluatorAuthorizationError
            If the canonical hash check or counterparty check fails.
        """
        # Lazy import to avoid circular at module load time.
        from core.primitives.evaluator import PrimaryEvaluator  # noqa: PLC0415

        # ------------------------------------------------------------------
        # Authorization checks (before touching the artifact or schema)
        # ------------------------------------------------------------------
        canonical_hash_required = sla.canonical_evaluator_hash
        if canonical_hash_required:
            if evaluator.canonical_hash != canonical_hash_required:
                raise EvaluatorAuthorizationError(
                    f"evaluator canonical_hash {evaluator.canonical_hash!r} "
                    f"does not match SLA canonical_evaluator_hash "
                    f"{canonical_hash_required!r}"
                )

        if evaluator.evaluator_did == sla.requester_node_did:
            raise EvaluatorAuthorizationError(
                f"evaluator_did {evaluator.evaluator_did!r} must not equal "
                f"requester_node_did {sla.requester_node_did!r}"
            )
        if evaluator.evaluator_did == sla.provider_node_did:
            raise EvaluatorAuthorizationError(
                f"evaluator_did {evaluator.evaluator_did!r} must not equal "
                f"provider_node_did {sla.provider_node_did!r}"
            )

        # ------------------------------------------------------------------
        # Mechanical gate: Tier 0 schema check
        # ------------------------------------------------------------------
        artifact_hash = hashlib.sha256(artifact_bytes).hexdigest()
        issued_at = _datetime_now_utc_z()

        schema_result, schema_evidence = self.schema_verifier.verify(
            sla, artifact_bytes, artifact_properties=artifact_properties
        )

        if schema_result != "accepted":
            # Mechanical fail: return a Tier 0 verdict; evaluator not called.
            tier0_evidence = dict(schema_evidence)
            tier0_evidence["tier1_skipped_via_mechanical_fail"] = True
            return OracleVerdict.create(
                sla_id=sla.sla_id,
                artifact_hash=artifact_hash,
                tier=0,
                result=schema_result,
                evaluator_did=self.node_did,
                evidence=tier0_evidence,
                issued_at=issued_at,
                signer=LocalKeypairSigner(self.node_keypair),
            )

        # ------------------------------------------------------------------
        # Evaluator call with wall-clock timeout
        # ------------------------------------------------------------------
        node_signer = LocalKeypairSigner(self.node_keypair)

        def _call_evaluator():
            return evaluator.evaluate(
                sla, artifact_bytes, artifact_properties=artifact_properties
            )

        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(_call_evaluator)
                output = future.result(timeout=self.evaluator_timeout_sec)
        except concurrent.futures.TimeoutError:
            timeout_evidence = {
                "kind": "evaluator_timeout",
                "detail": f"evaluator exceeded {self.evaluator_timeout_sec}s",
            }
            return OracleVerdict.create(
                sla_id=sla.sla_id,
                artifact_hash=artifact_hash,
                tier=1,
                result="refunded",
                evaluator_did=evaluator.evaluator_did,
                evidence=timeout_evidence,
                issued_at=issued_at,
                signer=node_signer,
                protocol_version="companyos-verdict/0.2",
                score=Decimal("0"),
            )

        # ------------------------------------------------------------------
        # Build Tier 1 verdict from evaluator output
        # ------------------------------------------------------------------
        tier1_evidence = {
            "kind": output.evidence["kind"],
            "evaluator_canonical_hash": output.evaluator_canonical_hash,
        }
        # Carry through any additional keys the evaluator included
        # (e.g. evaluator_error detail for refunded outputs).
        for key, value in output.evidence.items():
            if key != "kind":
                tier1_evidence[key] = value

        return OracleVerdict.create(
            sla_id=sla.sla_id,
            artifact_hash=artifact_hash,
            tier=1,
            result=output.result,
            evaluator_did=evaluator.evaluator_did,
            evidence=tier1_evidence,
            issued_at=issued_at,
            signer=node_signer,
            protocol_version="companyos-verdict/0.2",
            score=output.score,
        )

    def evaluate(
        self,
        sla: "InterOrgSLA",
        artifact_bytes: bytes,
        *,
        evaluator: "PrimaryEvaluator | None" = None,
        artifact_properties: dict | None = None,
    ) -> OracleVerdict:
        """Convenience dispatcher: routes to Tier 1 or Tier 0 based on SLA.

        If `sla.primary_evaluator_did` is set (non-empty string), calls
        `evaluate_tier1` with the supplied `evaluator`. If it is None or
        empty, calls `evaluate_tier0`.

        Parameters
        ----------
        sla:
            The InterOrgSLA governing this delivery.
        artifact_bytes:
            Raw bytes of the delivered artifact.
        evaluator:
            Required when `sla.primary_evaluator_did` is set. Ignored
            (and may be None) when routing to Tier 0.
        artifact_properties:
            Optional dict passed through to the chosen tier unchanged.

        Returns
        -------
        OracleVerdict

        Raises
        ------
        ValueError
            If Tier 1 is selected but `evaluator` is None.
        EvaluatorAuthorizationError
            Propagated from `evaluate_tier1` on authorization failures.
        """
        if sla.primary_evaluator_did:
            if evaluator is None:
                raise ValueError(
                    "evaluator is required when sla.primary_evaluator_did is set"
                )
            return self.evaluate_tier1(
                sla,
                artifact_bytes,
                evaluator=evaluator,
                artifact_properties=artifact_properties,
            )
        return self.evaluate_tier0(
            sla,
            artifact_bytes,
            artifact_properties=artifact_properties,
        )

