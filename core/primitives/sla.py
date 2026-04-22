"""
core/primitives/sla.py — InterOrgSLA primitive (unsigned + canonical hashing)
=============================================================================
Ticket 5 of the v0 Currency-Agnostic Settlement Architecture.

An `InterOrgSLA` is a frozen dataclass that captures the full terms of a
task the requester organization is asking the provider organization to
perform, priced and penalty-staked in currency-agnostic `Money`. It is
the contract object that flows between organizations before any on-chain
settlement is initiated.

This ticket lands the UNSIGNED structure + canonical byte representation
+ integrity binding. Ticket 8 adds signing (see `core.primitives.identity`).

Canonical Serialization Rules (spec, not suggestion)
----------------------------------------------------
The `_canonical_bytes` function is the SINGLE authoritative way to turn
an SLA into bytes. All hashing (integrity binding, eventually signatures)
MUST go through it. The rules:

1. JSON via `json.dumps(..., sort_keys=True, separators=(",", ":"),
   ensure_ascii=False)`. No whitespace. Keys sorted at every nesting
   level.
2. Nested dicts (including `deliverable_schema`) are recursively
   key-sorted by the same sort-keys mechanism.
3. `Decimal` values are emitted via `{d:f}` — fixed notation at the
   asset's precision — and NEVER as float / NEVER as scientific
   notation. `Money` already canonicalizes its internal Decimal during
   construction so `Money(Decimal("1"), usd)` and
   `Money(Decimal("1.000000"), usd)` hash identically.
4. Datetimes are always UTC-Z strings `"YYYY-MM-DDTHH:MM:SSZ"`:
     - aware `datetime` → converted to UTC, formatted `.strftime`.
     - ISO-8601 string with explicit offset → parsed, converted, reformatted.
     - naive datetime or offset-free string → ValueError.
5. `requester_signature` and `provider_signature` are ALWAYS excluded
   from the canonical bytes. That's how `InterOrgSLA.create` can
   populate the binding without triggering a chicken-and-egg.

`integrity_binding` Computation
-------------------------------
`integrity_binding` is a SHA-256 (via
`core.primitives.integrity.compute_integrity_hash`) over the canonical
bytes of the SLA with BOTH signatures AND the integrity_binding field
itself excluded (the shell form). Excluding the binding field during
its own computation dodges the chicken-and-egg; including everything
else ensures any tamper to task scope, money, nonce, timestamps, etc.
perturbs the hash.

Because the binding is a derived field, instances are created via
`InterOrgSLA.create(...)` — the strict factory computes the hash and
constructs the frozen dataclass with it filled. The raw constructor
is still available for `from_dict` rehydration but callers should
prefer `create`.
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping

import dataclasses

from core.primitives.asset import AssetRegistry
from core.primitives.exceptions import SignatureError
from core.primitives.identity import (
    Ed25519Keypair,
    Ed25519PublicKey,
    Signature,
    sign as _identity_sign,
    verify as _identity_verify,
)
from core.primitives.integrity import compute_integrity_hash
from core.primitives.money import Money


# ---------------------------------------------------------------------------
# Canonical datetime normalization
# ---------------------------------------------------------------------------
_UTC_Z_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def _canonicalize_datetime(value: Any) -> str:
    """Normalize a datetime-or-string to canonical `YYYY-MM-DDTHH:MM:SSZ`.

    Accepts:
      - `datetime` with `tzinfo` set (any timezone → converted to UTC).
      - ISO-8601 string with explicit offset, e.g.
        `"2026-04-19T08:00:00-04:00"` or `"2026-04-19T12:00:00Z"` or
        with fractional seconds `"2026-04-19T12:00:00.123+00:00"`
        (fractional seconds are TRUNCATED — canonical form has no
        sub-seconds).

    Rejects:
      - naive `datetime` (no tzinfo) → ValueError.
      - string without explicit offset, e.g. `"2026-04-19T12:00:00"` →
        ValueError.
      - any other type → TypeError.
    """
    if isinstance(value, datetime):
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise ValueError(
                "datetime must be timezone-aware (got naive). "
                "Pass a tz-aware datetime or an ISO-8601 string with an "
                "explicit offset like 'Z' or '+00:00'."
            )
        dt = value.astimezone(timezone.utc)
        return dt.strftime(_UTC_Z_FORMAT)

    if isinstance(value, str):
        # `datetime.fromisoformat` in 3.11+ accepts trailing 'Z'. For
        # older interpreters we normalize 'Z' to '+00:00' ourselves.
        candidate = value
        if candidate.endswith("Z"):
            candidate = candidate[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError as exc:
            raise ValueError(
                f"invalid ISO-8601 datetime string: {value!r} ({exc})"
            ) from exc
        if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
            raise ValueError(
                f"datetime string {value!r} has no timezone offset. "
                f"Include a 'Z' suffix or a numeric offset like '+00:00'."
            )
        dt = parsed.astimezone(timezone.utc)
        return dt.strftime(_UTC_Z_FORMAT)

    raise TypeError(
        f"datetime field must be `datetime` or ISO-8601 str, "
        f"got {type(value).__name__}"
    )


# ---------------------------------------------------------------------------
# Canonical serialization
# ---------------------------------------------------------------------------
# Fields that must NEVER appear in the canonical bytes input — they are
# signatures (Ticket 8) and are explicitly outside the hashed body.
_SIGNATURE_FIELDS = frozenset({"requester_signature", "provider_signature"})


def _json_default(obj: Any) -> Any:
    """JSON serializer for `Decimal` — every other non-standard type is
    already converted to a dict/str before we reach `json.dumps`."""
    if isinstance(obj, Decimal):
        # Fixed notation, at whatever precision the Decimal was
        # constructed with. The callers of `_canonical_bytes` always
        # pass Money through `Money.to_dict()` which already emits a
        # fixed-notation string, so this path is primarily a guard.
        return f"{obj:f}"
    raise TypeError(
        f"_canonical_bytes cannot serialize {type(obj).__name__}: "
        f"convert to dict/str/int/float/bool/None before canonicalization"
    )


def _sla_shell_dict(sla: "InterOrgSLA | Mapping[str, Any]") -> dict:
    """Build the dict representation used as input to the canonical
    JSON serializer.

    - Accepts either an `InterOrgSLA` instance or a dict-shaped shell
      (used during `create` before the binding exists).
    - Signature fields are dropped.
    - `payment` / `penalty_stake` (Money) are converted via
      `Money.to_dict()` so they land as
      `{"quantity": "...", "asset_id": "..."}`.
    - `deliverable_schema` is passed through unchanged — `json.dumps`
      with `sort_keys=True` handles recursive key sorting.
    """
    if isinstance(sla, InterOrgSLA):
        raw = {f.name: getattr(sla, f.name) for f in fields(sla)}
    else:
        raw = dict(sla)

    out: dict[str, Any] = {}
    for key, value in raw.items():
        if key in _SIGNATURE_FIELDS:
            continue
        if isinstance(value, Money):
            out[key] = value.to_dict()
        else:
            out[key] = value
    return out


def _canonical_bytes(sla: "InterOrgSLA | Mapping[str, Any]") -> bytes:
    """Canonical UTF-8 JSON bytes of an SLA. See module docstring."""
    shell = _sla_shell_dict(sla)
    return json.dumps(
        shell,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=_json_default,
    ).encode("utf-8")


def _canonical_bytes_for_binding(shell: Mapping[str, Any]) -> bytes:
    """Canonical bytes used to COMPUTE `integrity_binding`.

    Same as `_canonical_bytes` but also drops `integrity_binding` from
    the input so the hash input does not depend on its own output.
    Callers pass a raw dict shell (not an `InterOrgSLA`) because the
    frozen dataclass can't be constructed without a binding yet.
    """
    trimmed = {k: v for k, v in shell.items() if k != "integrity_binding"}
    return _canonical_bytes(trimmed)


# ---------------------------------------------------------------------------
# InterOrgSLA — the primitive
# ---------------------------------------------------------------------------
_PROTOCOL_VERSION_DEFAULT = "companyos-sla/0.1"

# v1a oracle additions (Ticket A3). Three fields are reserved for Tier 1
# (v1b) but are canonical now so future adapters see them; one is already
# consumed by v1a's SchemaVerifier.
#
# challenge_window_sec is validated at construction time. Bounds are:
#   - 60s  : shortest meaningful window for automated systems
#   - 604800s (7 days) : longest window before the contract stales out
_CHALLENGE_WINDOW_SEC_MIN = 60
_CHALLENGE_WINDOW_SEC_MAX = 604_800
_CHALLENGE_WINDOW_SEC_DEFAULT = 86_400  # 24 hours


@dataclass(frozen=True)
class InterOrgSLA:
    """Immutable inter-organization Service Level Agreement.

    Field order note
    ----------------
    Python frozen dataclasses require default-less fields first. The
    plan's semantic ordering (protocol_version before the payload) is
    preserved in `to_dict()` output via key sorting; the Python-level
    order below is driven purely by the default-vs-required split.
    """

    # --- required -----------------------------------------------------------
    sla_id: str
    requester_node_did: str
    provider_node_did: str
    task_scope: str
    deliverable_schema: dict
    accuracy_requirement: float
    latency_ms: int
    payment: Money
    penalty_stake: Money
    nonce: str
    issued_at: str
    expires_at: str
    integrity_binding: str

    # --- defaulted ---------------------------------------------------------
    protocol_version: str = _PROTOCOL_VERSION_DEFAULT
    protocol_fee_bps: int = 0

    # v1a oracle fields (Ticket A3). `artifact_hash_at_delivery` is
    # populated by the provider at delivery time via `with_delivery_hash`;
    # empty string at signing time is permitted. The other three are
    # reserved for Tier 1 consumers in v1b and carried here so the
    # canonical byte shape stabilizes before Tier 1 ships.
    artifact_hash_at_delivery: str = ""
    primary_evaluator_did: "str | None" = None
    canonical_evaluator_hash: "str | None" = None
    primary_evaluator_pubkey_hex: str = ""
    challenge_window_sec: int = _CHALLENGE_WINDOW_SEC_DEFAULT

    requester_signature: "Signature | None" = None
    provider_signature: "Signature | None" = None

    # ------------------------------------------------------------------
    # Factories
    # ------------------------------------------------------------------
    @classmethod
    def create(
        cls,
        *,
        sla_id: str,
        requester_node_did: str,
        provider_node_did: str,
        task_scope: str,
        deliverable_schema: dict,
        accuracy_requirement: float,
        latency_ms: int,
        payment: Money,
        penalty_stake: Money,
        nonce: str,
        issued_at: Any,
        expires_at: Any,
        protocol_fee_bps: int = 0,
        protocol_version: str = _PROTOCOL_VERSION_DEFAULT,
        artifact_hash_at_delivery: str = "",
        primary_evaluator_did: "str | None" = None,
        canonical_evaluator_hash: "str | None" = None,
        primary_evaluator_pubkey_hex: str = "",
        challenge_window_sec: int = _CHALLENGE_WINDOW_SEC_DEFAULT,
    ) -> "InterOrgSLA":
        """Strict factory — validates inputs, normalizes timestamps,
        computes `integrity_binding`, returns the fully-filled frozen
        SLA with both signature fields left `None`.

        Signatures are never set here. Ticket 8 adds an
        `InterOrgSLA.with_requester_signature(...)` helper that
        rebuilds the frozen instance with the requester signature
        populated (similar for the provider).
        """
        # --- validate scalars -----------------------------------------------
        if not isinstance(sla_id, str) or not sla_id:
            raise ValueError("sla_id must be a non-empty string")
        if not isinstance(requester_node_did, str) or not requester_node_did:
            raise ValueError("requester_node_did must be a non-empty string")
        if not isinstance(provider_node_did, str) or not provider_node_did:
            raise ValueError("provider_node_did must be a non-empty string")
        if not isinstance(task_scope, str) or not task_scope:
            raise ValueError("task_scope must be a non-empty string")
        if not isinstance(deliverable_schema, dict):
            raise TypeError(
                f"deliverable_schema must be dict, got "
                f"{type(deliverable_schema).__name__}"
            )
        if not isinstance(accuracy_requirement, (int, float)):
            raise TypeError("accuracy_requirement must be float or int")
        if not isinstance(latency_ms, int) or latency_ms < 0:
            raise ValueError("latency_ms must be a non-negative int")
        if not isinstance(payment, Money):
            raise TypeError("payment must be a Money instance")
        if not isinstance(penalty_stake, Money):
            raise TypeError("penalty_stake must be a Money instance")
        if not isinstance(nonce, str) or not nonce:
            raise ValueError("nonce must be a non-empty string")
        if not isinstance(protocol_fee_bps, int) or protocol_fee_bps < 0:
            raise ValueError("protocol_fee_bps must be a non-negative int")

        # --- v1a oracle field validation ------------------------------------
        if not isinstance(artifact_hash_at_delivery, str):
            raise TypeError(
                "artifact_hash_at_delivery must be a string "
                "(empty string is permitted at signing time)"
            )
        if not isinstance(primary_evaluator_pubkey_hex, str):
            raise TypeError(
                "primary_evaluator_pubkey_hex must be a string (empty string is "
                "the Tier 0 default)"
            )
        # Explicit empty-string rejection for the str|None fields: an empty
        # string is neither a valid DID/hash nor a valid "not set" sentinel
        # (None is the canonical "not set" value for those fields).
        if primary_evaluator_did is not None and (
            not isinstance(primary_evaluator_did, str) or not primary_evaluator_did
        ):
            raise ValueError(
                "primary_evaluator_did, when provided, must be a non-empty string"
            )
        if canonical_evaluator_hash is not None and (
            not isinstance(canonical_evaluator_hash, str)
            or not canonical_evaluator_hash
        ):
            raise ValueError(
                "canonical_evaluator_hash, when provided, must be a non-empty string"
            )

        # --- Tier 1 coupling validation (B0-d) ------------------------------
        # All three evaluator identity fields must be set together or all
        # left empty. Partial population is an error.
        _tier1_fields_set = [
            bool(primary_evaluator_did),
            bool(canonical_evaluator_hash),
            bool(primary_evaluator_pubkey_hex),
        ]
        if any(_tier1_fields_set) and not all(_tier1_fields_set):
            raise ValueError(
                "primary_evaluator_did, canonical_evaluator_hash, and "
                "primary_evaluator_pubkey_hex must all be set together "
                "(Tier 1 coupling) or all be left empty (Tier 0 default)."
            )

        if all(_tier1_fields_set):
            # primary_evaluator_did: non-empty string (already guaranteed by
            # bool() check above, but validate type explicitly)
            if not isinstance(primary_evaluator_did, str) or not primary_evaluator_did:
                raise ValueError(
                    "primary_evaluator_did, when provided, must be a non-empty string"
                )
            # canonical_evaluator_hash: exactly 64 lowercase hex chars
            if not isinstance(canonical_evaluator_hash, str) or not re.fullmatch(
                r"[0-9a-f]{64}", canonical_evaluator_hash
            ):
                raise ValueError(
                    "canonical_evaluator_hash must be exactly 64 lowercase hex "
                    "characters when set"
                )
            # primary_evaluator_pubkey_hex: exactly 64 lowercase hex chars
            if not re.fullmatch(r"[0-9a-f]{64}", primary_evaluator_pubkey_hex):
                raise ValueError(
                    "primary_evaluator_pubkey_hex must be exactly 64 lowercase hex "
                    "characters (Ed25519 public key) when set"
                )

        if not isinstance(challenge_window_sec, int) or isinstance(
            challenge_window_sec, bool
        ):
            raise TypeError("challenge_window_sec must be an int")
        if not (
            _CHALLENGE_WINDOW_SEC_MIN
            <= challenge_window_sec
            <= _CHALLENGE_WINDOW_SEC_MAX
        ):
            raise ValueError(
                f"challenge_window_sec must be in "
                f"[{_CHALLENGE_WINDOW_SEC_MIN}, {_CHALLENGE_WINDOW_SEC_MAX}], "
                f"got {challenge_window_sec}"
            )

        # --- normalize datetimes --------------------------------------------
        issued_at_str = _canonicalize_datetime(issued_at)
        expires_at_str = _canonicalize_datetime(expires_at)

        # --- build shell, compute binding -----------------------------------
        shell: dict[str, Any] = {
            "sla_id": sla_id,
            "requester_node_did": requester_node_did,
            "provider_node_did": provider_node_did,
            "task_scope": task_scope,
            "deliverable_schema": deliverable_schema,
            "accuracy_requirement": float(accuracy_requirement),
            "latency_ms": latency_ms,
            "payment": payment,
            "penalty_stake": penalty_stake,
            "nonce": nonce,
            "issued_at": issued_at_str,
            "expires_at": expires_at_str,
            "protocol_version": protocol_version,
            "protocol_fee_bps": protocol_fee_bps,
            "artifact_hash_at_delivery": artifact_hash_at_delivery,
            "primary_evaluator_did": primary_evaluator_did,
            "canonical_evaluator_hash": canonical_evaluator_hash,
            "primary_evaluator_pubkey_hex": primary_evaluator_pubkey_hex,
            "challenge_window_sec": challenge_window_sec,
        }
        body_bytes = _canonical_bytes_for_binding(shell)
        # `compute_integrity_hash` takes a `body: str`. Decode the
        # canonical bytes as UTF-8 (safe — we produced them with
        # ensure_ascii=False + encode("utf-8")) so we reuse the
        # existing primitive without adding a second hashing path.
        binding = compute_integrity_hash(
            body=body_bytes.decode("utf-8"),
            provenance={
                "kind": "inter-org-sla",
                "protocol_version": protocol_version,
            },
        )

        return cls(
            sla_id=sla_id,
            requester_node_did=requester_node_did,
            provider_node_did=provider_node_did,
            task_scope=task_scope,
            deliverable_schema=deliverable_schema,
            accuracy_requirement=float(accuracy_requirement),
            latency_ms=latency_ms,
            payment=payment,
            penalty_stake=penalty_stake,
            nonce=nonce,
            issued_at=issued_at_str,
            expires_at=expires_at_str,
            integrity_binding=binding,
            protocol_version=protocol_version,
            protocol_fee_bps=protocol_fee_bps,
            artifact_hash_at_delivery=artifact_hash_at_delivery,
            primary_evaluator_did=primary_evaluator_did,
            canonical_evaluator_hash=canonical_evaluator_hash,
            primary_evaluator_pubkey_hex=primary_evaluator_pubkey_hex,
            challenge_window_sec=challenge_window_sec,
            requester_signature=None,
            provider_signature=None,
        )

    def with_delivery_hash(self, artifact_hash: str) -> "InterOrgSLA":
        """Return a copy of this SLA with `artifact_hash_at_delivery`
        populated and the `integrity_binding` recomputed.

        Called by the provider at delivery time to bind the delivered
        artifact bytes to the SLA. Because `artifact_hash_at_delivery`
        participates in canonical bytes, updating it changes the binding
        AND invalidates any prior signatures (which sign over the old
        binding). Callers that need re-signed proof must call
        `sign_as_requester` / `sign_as_provider` on the returned SLA.

        Raises:
            ValueError: if `artifact_hash` is empty or not a string.
        """
        if not isinstance(artifact_hash, str) or not artifact_hash:
            raise ValueError("artifact_hash must be a non-empty string")
        with_hash = dataclasses.replace(
            self, artifact_hash_at_delivery=artifact_hash
        )
        return dataclasses.replace(
            with_hash, integrity_binding=with_hash.recompute_binding()
        )

    @staticmethod
    def new_nonce() -> str:
        """Convenience helper — returns a 32-char uuid4 hex string."""
        return uuid.uuid4().hex

    # ------------------------------------------------------------------
    # Hashing / verification
    # ------------------------------------------------------------------
    def canonical_bytes(self) -> bytes:
        """Public handle onto the canonical byte representation.

        Includes `integrity_binding`, excludes both signature fields.
        Ticket 8 will sign these bytes.
        """
        return _canonical_bytes(self)

    def recompute_binding(self) -> str:
        """Recompute `integrity_binding` from current field values.

        `verify_binding()` compares this against `self.integrity_binding`.
        """
        shell = _sla_shell_dict(self)
        body_bytes = _canonical_bytes_for_binding(shell)
        return compute_integrity_hash(
            body=body_bytes.decode("utf-8"),
            provenance={
                "kind": "inter-org-sla",
                "protocol_version": self.protocol_version,
            },
        )

    def verify_binding(self) -> bool:
        """True iff `integrity_binding` matches the canonical bytes."""
        return self.recompute_binding() == self.integrity_binding

    # ------------------------------------------------------------------
    # Signing (Ticket 8)
    # ------------------------------------------------------------------
    def _canonical_bytes_for_signing(self) -> bytes:
        """Canonical bytes used as the signed payload.

        Because `_canonical_bytes` already excludes BOTH signature
        fields, the bytes a signer produces are identical regardless of
        whether the other party has already signed. That makes the two
        signature paths fully independent — order of signing never
        affects either byte input, so an SLA can be co-signed by the
        requester and provider in any order (or even concurrently).
        """
        return _canonical_bytes(self)

    def sign_as_requester(self, keypair: Ed25519Keypair) -> "InterOrgSLA":
        """Return a copy of this SLA with `requester_signature` populated.

        The canonical bytes used for signing explicitly exclude both
        signature fields (see `_canonical_bytes_for_signing`). That
        invariant, combined with the frozen-dataclass substitution via
        `dataclasses.replace`, means this method is safe to call before
        OR after `sign_as_provider` — neither ordering changes the
        bytes the other party signs.
        """
        sig = _identity_sign(keypair, self._canonical_bytes_for_signing())
        return dataclasses.replace(self, requester_signature=sig)

    def sign_as_provider(self, keypair: Ed25519Keypair) -> "InterOrgSLA":
        """Return a copy of this SLA with `provider_signature` populated.

        See `sign_as_requester` for why signing order is immaterial.
        """
        sig = _identity_sign(keypair, self._canonical_bytes_for_signing())
        return dataclasses.replace(self, provider_signature=sig)

    def verify_signatures(
        self,
        *,
        registry: "object | None" = None,
        requester_pubkey: "Ed25519PublicKey | None" = None,
        provider_pubkey: "Ed25519PublicKey | None" = None,
    ) -> None:
        """Verify BOTH signatures on this SLA.

        Two modes — caller picks exactly one:
          (a) explicit pubkeys via `requester_pubkey` / `provider_pubkey`
              (Ticket 8 — implemented here).
          (b) node registry lookup via `registry: NodeRegistry`
              (Ticket 10 — wires up the registry branch below).

        On success, returns None. On failure, raises `SignatureError`
        with a message identifying the specific failure mode
        (missing / wrong-signer / invalid-bytes).

        A `TypeError` is raised for ambiguous / empty mode selection,
        so a caller that forgets to pass ANY pubkey source does not
        silently "pass" verification against a partially-built SLA.
        """
        # Mode selection: exactly one of (registry) or (both pubkeys).
        has_registry = registry is not None
        has_explicit = requester_pubkey is not None and provider_pubkey is not None
        has_partial_explicit = (
            (requester_pubkey is not None) ^ (provider_pubkey is not None)
        )

        if has_registry and (has_explicit or has_partial_explicit):
            raise TypeError(
                "verify_signatures: pass EITHER registry OR "
                "(requester_pubkey + provider_pubkey), not both"
            )
        if has_partial_explicit:
            raise TypeError(
                "verify_signatures: both requester_pubkey and "
                "provider_pubkey are required when using explicit-pubkey mode"
            )
        if not has_registry and not has_explicit:
            raise TypeError(
                "verify_signatures: pass either registry or "
                "(requester_pubkey + provider_pubkey)"
            )

        if has_registry:
            # Ticket 10: resolve requester_node_did / provider_node_did
            # through the NodeRegistry to produce the expected
            # (requester_pubkey, provider_pubkey). Unknown DIDs surface
            # as SignatureError so callers get a single exception type
            # back regardless of the lookup mode.
            try:
                requester_pubkey = registry.get(self.requester_node_did)
            except KeyError:
                raise SignatureError(
                    f"unknown counterparty: {self.requester_node_did} "
                    f"not in NodeRegistry"
                ) from None
            try:
                provider_pubkey = registry.get(self.provider_node_did)
            except KeyError:
                raise SignatureError(
                    f"unknown counterparty: {self.provider_node_did} "
                    f"not in NodeRegistry"
                ) from None

        # --- Presence ---------------------------------------------------
        if self.requester_signature is None:
            raise SignatureError("Requester signature missing")
        if self.provider_signature is None:
            raise SignatureError("Provider signature missing")

        # --- Signer identity ------------------------------------------
        # The signer pubkey embedded in the signature must match the
        # expected party. This is what prevents swapping two signatures
        # at rest (e.g., labeling a provider-signed blob as requester),
        # AND — in registry mode — is the defense against Sybil pubkeys:
        # a keypair_B signing as `did:companyos:X` fails here because
        # registry.get("did:...:X") returns pubkey_A.
        if self.requester_signature.signer != requester_pubkey:
            if has_registry:
                raise SignatureError(
                    f"signer pubkey does not match registered pubkey for "
                    f"{self.requester_node_did}"
                )
            raise SignatureError(
                "Requester signature signer does not match expected "
                "requester pubkey"
            )
        if self.provider_signature.signer != provider_pubkey:
            if has_registry:
                raise SignatureError(
                    f"signer pubkey does not match registered pubkey for "
                    f"{self.provider_node_did}"
                )
            raise SignatureError(
                "Provider signature signer does not match expected "
                "provider pubkey"
            )

        # --- Cryptographic verification -------------------------------
        body = self._canonical_bytes_for_signing()
        if not _identity_verify(self.requester_signature, body):
            raise SignatureError(
                "Requester signature failed cryptographic verification"
            )
        if not _identity_verify(self.provider_signature, body):
            raise SignatureError(
                "Provider signature failed cryptographic verification"
            )

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------
    def to_dict(self) -> dict:
        """Emit a fully-serializable dict.

        Signatures ARE included here (so callers can persist them) —
        they just don't participate in canonical bytes.
        """
        out: dict[str, Any] = {
            "sla_id": self.sla_id,
            "requester_node_did": self.requester_node_did,
            "provider_node_did": self.provider_node_did,
            "task_scope": self.task_scope,
            "deliverable_schema": self.deliverable_schema,
            "accuracy_requirement": self.accuracy_requirement,
            "latency_ms": self.latency_ms,
            "payment": self.payment.to_dict(),
            "penalty_stake": self.penalty_stake.to_dict(),
            "nonce": self.nonce,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "integrity_binding": self.integrity_binding,
            "protocol_version": self.protocol_version,
            "protocol_fee_bps": self.protocol_fee_bps,
            "artifact_hash_at_delivery": self.artifact_hash_at_delivery,
            "primary_evaluator_did": self.primary_evaluator_did,
            "canonical_evaluator_hash": self.canonical_evaluator_hash,
            "primary_evaluator_pubkey_hex": self.primary_evaluator_pubkey_hex,
            "challenge_window_sec": self.challenge_window_sec,
            "requester_signature": (
                self.requester_signature.to_dict()
                if self.requester_signature is not None
                else None
            ),
            "provider_signature": (
                self.provider_signature.to_dict()
                if self.provider_signature is not None
                else None
            ),
        }
        return out

    @classmethod
    def from_dict(cls, d: Mapping[str, Any], registry: AssetRegistry) -> "InterOrgSLA":
        """Rehydrate from a `to_dict` payload + an AssetRegistry.

        The registry resolves `asset_id` → `AssetRef` for both
        `payment` and `penalty_stake`. Signatures round-trip if
        present; absent/`None` signatures stay `None`. We do NOT
        recompute the binding here — the stored value is preserved
        verbatim so callers can distinguish "tampered" from
        "structurally invalid" via `verify_binding()`.
        """
        required = (
            "sla_id",
            "requester_node_did",
            "provider_node_did",
            "task_scope",
            "deliverable_schema",
            "accuracy_requirement",
            "latency_ms",
            "payment",
            "penalty_stake",
            "nonce",
            "issued_at",
            "expires_at",
            "integrity_binding",
        )
        for key in required:
            if key not in d:
                raise ValueError(f"InterOrgSLA.from_dict missing field {key!r}")

        payment_payload = d["payment"]
        penalty_payload = d["penalty_stake"]
        payment = Money.from_dict(
            payment_payload,
            registry.get(payment_payload["asset_id"]),
        )
        penalty = Money.from_dict(
            penalty_payload,
            registry.get(penalty_payload["asset_id"]),
        )

        req_sig = d.get("requester_signature")
        prov_sig = d.get("provider_signature")
        primary_eval = d.get("primary_evaluator_did")
        canonical_eval_hash = d.get("canonical_evaluator_hash")
        return cls(
            sla_id=str(d["sla_id"]),
            requester_node_did=str(d["requester_node_did"]),
            provider_node_did=str(d["provider_node_did"]),
            task_scope=str(d["task_scope"]),
            deliverable_schema=dict(d["deliverable_schema"]),
            accuracy_requirement=float(d["accuracy_requirement"]),
            latency_ms=int(d["latency_ms"]),
            payment=payment,
            penalty_stake=penalty,
            nonce=str(d["nonce"]),
            issued_at=str(d["issued_at"]),
            expires_at=str(d["expires_at"]),
            integrity_binding=str(d["integrity_binding"]),
            protocol_version=str(
                d.get("protocol_version", _PROTOCOL_VERSION_DEFAULT)
            ),
            protocol_fee_bps=int(d.get("protocol_fee_bps", 0)),
            artifact_hash_at_delivery=str(d.get("artifact_hash_at_delivery", "")),
            primary_evaluator_did=(
                str(primary_eval) if primary_eval is not None else None
            ),
            canonical_evaluator_hash=(
                str(canonical_eval_hash)
                if canonical_eval_hash is not None
                else None
            ),
            primary_evaluator_pubkey_hex=str(
                d.get("primary_evaluator_pubkey_hex", "")
            ),
            challenge_window_sec=int(
                d.get("challenge_window_sec", _CHALLENGE_WINDOW_SEC_DEFAULT)
            ),
            requester_signature=(
                Signature.from_dict(req_sig) if req_sig is not None else None
            ),
            provider_signature=(
                Signature.from_dict(prov_sig) if prov_sig is not None else None
            ),
        )


__all__ = ["InterOrgSLA"]
