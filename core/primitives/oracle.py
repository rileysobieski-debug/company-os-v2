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

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal, get_args

from core.primitives.exceptions import SignatureError, VerdictError
from core.primitives.identity import (
    Ed25519Keypair,
    Ed25519PublicKey,
    Signature,
    sign as _identity_sign,
    verify as _identity_verify,
)

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
    """
    shell = _verdict_shell_dict(verdict, exclude_verdict_hash=exclude_verdict_hash)
    return json.dumps(
        shell,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=_json_default,
    ).encode("utf-8")


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
        keypair: Ed25519Keypair,
        protocol_version: str = _PROTOCOL_VERSION_DEFAULT,
        score: "Decimal | None" = None,
    ) -> "OracleVerdict":
        """Strict factory: validate, hash, sign, and return a frozen verdict.

        `keypair` provides both the signature and the embedded `signer`
        public key. There is exactly one signer per verdict (unlike the SLA
        where requester + provider both sign independently).

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
        if not isinstance(keypair, Ed25519Keypair):
            raise TypeError(
                f"keypair must be an Ed25519Keypair, got {type(keypair).__name__}"
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
            "signer": keypair.public_key,
            # signature excluded by _canonical_bytes
            "issued_at": issued_at,
            "protocol_version": protocol_version,
            "score": score,
        }
        body = _canonical_bytes(shell, exclude_verdict_hash=True)
        verdict_hash = hashlib.sha256(body).hexdigest()

        # Include verdict_hash in the bytes that get signed so the signature
        # commits to the hash (and therefore to all content fields).
        shell_with_hash = dict(shell, verdict_hash=verdict_hash)
        signing_body = _canonical_bytes(shell_with_hash, exclude_verdict_hash=False)
        sig = _identity_sign(keypair, signing_body)

        return cls(
            sla_id=sla_id,
            artifact_hash=artifact_hash,
            tier=tier,
            result=result,
            evaluator_did=evaluator_did,
            evidence=evidence,
            verdict_hash=verdict_hash,
            signer=keypair.public_key,
            signature=sig,
            issued_at=issued_at,
            protocol_version=protocol_version,
            score=score,
        )

    # -----------------------------------------------------------------------
    # Signature verification
    # -----------------------------------------------------------------------
    def verify_signature(self) -> None:
        """Verify the embedded Ed25519 signature over canonical bytes.

        Recomputes canonical bytes (signature excluded, verdict_hash
        included) and checks the signature against `self.signer`.

        Raises:
            SignatureError: if the signer embedded in `self.signature`
                does not match `self.signer`, or if cryptographic
                verification fails (tampered fields, wrong keypair, etc.).
        """
        # Signer consistency: the pubkey in the Signature must equal the
        # top-level `signer` field. This prevents signature/signer drift
        # where someone swaps one field without the other.
        if self.signature.signer != self.signer:
            raise SignatureError(
                "signature.signer does not match top-level signer field"
            )

        # Recompute the bytes that were signed in `create`: canonical bytes
        # with verdict_hash included and signature excluded.
        signing_body = _canonical_bytes(self, exclude_verdict_hash=False)
        if not _identity_verify(self.signature, signing_body):
            raise SignatureError(
                "OracleVerdict signature failed cryptographic verification"
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
]
