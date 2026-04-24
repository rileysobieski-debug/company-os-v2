"""Pure-Python deterministic evaluator (the Brain).

Core contract: an `ActionRequest` never becomes a dispatch. It either
returns APPROVE (allowed, dispatch may proceed), AUTO_DENY (refused;
the `reason` says why), or ESCALATE (refused at the evaluator level;
the `EscalationManifest` names who may sign an override and what
evidence they must cite).

Key design invariants:

    1. No LLM calls inside. Every gate is deterministic Python. An
       LLM can propose an action; it cannot decide one.

    2. Gate order is fixed and explicit. Each gate either passes,
       escalates, or denies. The first non-pass outcome short-circuits
       the remaining gates so a denied request is never accidentally
       approved by a later gate.

    3. Failure emits an `EscalationManifest`, not just a boolean. The
       M&A audit property is `the system recorded who authorized
       deviations and what evidence they cited`, not `the system
       blocked`.

    4. The manifest is content-addressed. `request_hash` is a SHA-256
       over the canonical ActionRequest so an override cannot retro-
       attach to a different request.

KMS signing of manifests is the v6 hardware-root-of-trust path. This
module ships the manifest shape + a pluggable `sign` hook; the actual
AWS KMS / HashiCorp Vault integration lands in Weeks 9-12. A local
dev signer is provided for tests.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Callable, Protocol
from uuid import uuid4

from core.governance.action_types import (
    ActionTier,
    ActionTypeRegistry,
    ActionTypeSpec,
    ActionTypeUnknown,
    get_default_registry,
)


class Verdict(Enum):
    APPROVE = "approve"
    AUTO_DENY = "auto_deny"
    ESCALATE = "escalate"


class Source(Enum):
    FOUNDER = "founder"
    AGENT = "agent"
    ORCHESTRATOR = "orchestrator"
    BOARD = "board"


class DenyReason(Enum):
    UNKNOWN_ACTION = "unknown_action_type"
    SCHEMA_INVALID = "schema_invalid"
    DORMANT = "action_dormant"
    RATE_LIMITED = "rate_limited"
    BUDGET_EXCEEDED = "budget_exceeded"
    EVIDENCE_MISSING = "evidence_missing"
    TRUST_INSUFFICIENT = "trust_insufficient"
    AUTONOMY_INSUFFICIENT = "autonomy_insufficient"
    HARD_CONSTRAINT = "hard_constraint"


@dataclass(frozen=True)
class Citation:
    """A link to the provenance of a claim used in this request.
    `hash` is the canonicalizer-v1 hash of the source artifact at
    capture time; the Memory layer re-verifies on execution."""
    source_path: str
    content_hash: str
    canonicalizer_version: str = "v1"
    kind: str = "generic"


@dataclass(frozen=True)
class ActionRequest:
    action_type: str
    source: Source
    payload: dict
    citations: tuple[Citation, ...]
    requested_at: str
    request_id: str = field(default_factory=lambda: str(uuid4()))
    estimated_cost_usd_cents: int = 0
    source_identity: str = ""  # e.g. "manager:marketing" or "founder:riley"

    def canonical_bytes(self) -> bytes:
        payload = {
            "action_type": self.action_type,
            "source": self.source.value,
            "payload": self.payload,
            "citations": [asdict(c) for c in self.citations],
            "requested_at": self.requested_at,
            "request_id": self.request_id,
            "estimated_cost_usd_cents": self.estimated_cost_usd_cents,
            "source_identity": self.source_identity,
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")

    def request_hash(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


@dataclass(frozen=True)
class EscalationManifest:
    request_hash: str
    action_type: str
    authorized_principals: tuple[str, ...]
    required_evidence_kinds: tuple[str, ...]
    expires_at: str
    reason_code: DenyReason
    reason_detail: str
    signature: str = ""  # populated by `Signer`
    signer_fingerprint: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["reason_code"] = self.reason_code.value
        return d


@dataclass(frozen=True)
class EvaluatorDecision:
    verdict: Verdict
    request_hash: str
    reason: str
    reason_code: DenyReason | None = None
    manifest: EscalationManifest | None = None


# ---------------------------------------------------------------------------
# Signer
# ---------------------------------------------------------------------------
class Signer(Protocol):
    def sign(self, payload: bytes) -> str: ...
    def fingerprint(self) -> str: ...


class LocalDevSigner:
    """In-process signer for tests and local development. Uses SHA-256
    over a per-instance keyring byte so the signature is stable within
    a process. NOT FOR PRODUCTION; the v6 plan's Hardware Root of Trust
    requires AWS KMS or HashiCorp Vault."""

    def __init__(self, key: bytes | None = None) -> None:
        self._key = key or uuid4().bytes

    def sign(self, payload: bytes) -> str:
        return hashlib.sha256(self._key + payload).hexdigest()

    def fingerprint(self) -> str:
        return hashlib.sha256(self._key).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Rate limiter (in-memory; DLQ-backed persistence is Weeks 9-12)
# ---------------------------------------------------------------------------
class RateLimiter:
    """Sliding-window rate limiter keyed by `(source_identity, action_type)`.
    Thread-safe. Window size is caller-supplied so different actions can
    have different cadences."""

    def __init__(self) -> None:
        self._events: dict[tuple[str, str], deque[float]] = {}
        self._lock = threading.Lock()

    def allow(
        self,
        *,
        source_identity: str,
        action_type: str,
        limit: int,
        window_seconds: float,
        now: float | None = None,
    ) -> bool:
        now = now if now is not None else time.monotonic()
        key = (source_identity, action_type)
        with self._lock:
            bucket = self._events.setdefault(key, deque())
            cutoff = now - window_seconds
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            if len(bucket) >= limit:
                return False
            bucket.append(now)
            return True

    def reset(self) -> None:
        with self._lock:
            self._events.clear()


# ---------------------------------------------------------------------------
# Autonomy matrix (Source x ActionTier -> Verdict)
# ---------------------------------------------------------------------------
_AUTONOMY: dict[tuple[Source, ActionTier], Verdict] = {
    (Source.AGENT, ActionTier.ROUTINE): Verdict.APPROVE,
    (Source.AGENT, ActionTier.TRUSTED): Verdict.APPROVE,
    (Source.AGENT, ActionTier.ELEVATED): Verdict.ESCALATE,
    (Source.AGENT, ActionTier.BOARD): Verdict.AUTO_DENY,
    (Source.ORCHESTRATOR, ActionTier.ROUTINE): Verdict.APPROVE,
    (Source.ORCHESTRATOR, ActionTier.TRUSTED): Verdict.APPROVE,
    (Source.ORCHESTRATOR, ActionTier.ELEVATED): Verdict.ESCALATE,
    (Source.ORCHESTRATOR, ActionTier.BOARD): Verdict.ESCALATE,
    (Source.BOARD, ActionTier.ROUTINE): Verdict.APPROVE,
    (Source.BOARD, ActionTier.TRUSTED): Verdict.APPROVE,
    (Source.BOARD, ActionTier.ELEVATED): Verdict.APPROVE,
    (Source.BOARD, ActionTier.BOARD): Verdict.ESCALATE,
    (Source.FOUNDER, ActionTier.ROUTINE): Verdict.APPROVE,
    (Source.FOUNDER, ActionTier.TRUSTED): Verdict.APPROVE,
    (Source.FOUNDER, ActionTier.ELEVATED): Verdict.APPROVE,
    (Source.FOUNDER, ActionTier.BOARD): Verdict.APPROVE,
}


def _autonomy_verdict(source: Source, tier: ActionTier) -> Verdict:
    return _AUTONOMY.get((source, tier), Verdict.AUTO_DENY)


# ---------------------------------------------------------------------------
# Evaluator context
# ---------------------------------------------------------------------------
@dataclass
class EvaluatorContext:
    """Per-tenant evaluator context. Composed by the dispatcher at
    request time. `hard_constraints` is typically sourced from the
    tenant's `TenantConfig.hard_constraints` list."""
    registry: ActionTypeRegistry | None = None
    rate_limiter: RateLimiter | None = None
    signer: Signer | None = None
    hard_constraints: tuple[str, ...] = field(default_factory=tuple)
    required_approvals: frozenset[str] = field(default_factory=frozenset)
    budget_remaining_usd_cents: int | None = None
    trust_score_lookup: Callable[[str], float] | None = None
    min_trust_by_tier: dict[ActionTier, float] = field(default_factory=dict)
    escalation_window: timedelta = field(default_factory=lambda: timedelta(hours=24))
    rate_policy: dict[str, tuple[int, float]] = field(default_factory=dict)

    def active_registry(self) -> ActionTypeRegistry:
        return self.registry or get_default_registry()

    def active_rate_limiter(self) -> RateLimiter:
        if self.rate_limiter is None:
            self.rate_limiter = RateLimiter()
        return self.rate_limiter


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------
def _build_manifest(
    *,
    request: ActionRequest,
    spec: ActionTypeSpec | None,
    reason_code: DenyReason,
    reason_detail: str,
    context: EvaluatorContext,
    authorized_principals: tuple[str, ...],
) -> EscalationManifest:
    expires_at = (
        datetime.now(timezone.utc) + context.escalation_window
    ).isoformat()
    manifest = EscalationManifest(
        request_hash=request.request_hash(),
        action_type=request.action_type,
        authorized_principals=authorized_principals,
        required_evidence_kinds=tuple(
            spec.required_evidence_kinds if spec else ()
        ),
        expires_at=expires_at,
        reason_code=reason_code,
        reason_detail=reason_detail,
    )
    if context.signer is not None:
        payload = json.dumps(manifest.to_dict(), sort_keys=True).encode("utf-8")
        return EscalationManifest(
            **{**manifest.to_dict(),
               "reason_code": reason_code,
               "signature": context.signer.sign(payload),
               "signer_fingerprint": context.signer.fingerprint()},
        )
    return manifest


def _deny(
    *,
    request: ActionRequest,
    reason_code: DenyReason,
    detail: str,
) -> EvaluatorDecision:
    return EvaluatorDecision(
        verdict=Verdict.AUTO_DENY,
        request_hash=request.request_hash(),
        reason=detail,
        reason_code=reason_code,
    )


def _escalate(
    *,
    request: ActionRequest,
    spec: ActionTypeSpec,
    reason_code: DenyReason,
    detail: str,
    context: EvaluatorContext,
    principals: tuple[str, ...] = ("founder",),
) -> EvaluatorDecision:
    manifest = _build_manifest(
        request=request,
        spec=spec,
        reason_code=reason_code,
        reason_detail=detail,
        context=context,
        authorized_principals=principals,
    )
    return EvaluatorDecision(
        verdict=Verdict.ESCALATE,
        request_hash=request.request_hash(),
        reason=detail,
        reason_code=reason_code,
        manifest=manifest,
    )


def _approve(request: ActionRequest) -> EvaluatorDecision:
    return EvaluatorDecision(
        verdict=Verdict.APPROVE,
        request_hash=request.request_hash(),
        reason="",
    )


# ---------------------------------------------------------------------------
# evaluate()
# ---------------------------------------------------------------------------
def evaluate(
    request: ActionRequest,
    *,
    context: EvaluatorContext | None = None,
) -> EvaluatorDecision:
    """Run the deterministic gate stack. Returns an `EvaluatorDecision`
    the dispatcher must honor: APPROVE -> proceed, AUTO_DENY -> refuse,
    ESCALATE -> wait for a signed override referencing the manifest."""
    context = context or EvaluatorContext()
    registry = context.active_registry()

    # Gate 1: known action type.
    try:
        spec = registry.get(request.action_type)
    except ActionTypeUnknown:
        return _deny(
            request=request,
            reason_code=DenyReason.UNKNOWN_ACTION,
            detail=f"action_type {request.action_type!r} not in registry",
        )

    # Gate 2: dormancy.
    if spec.dormancy:
        return _deny(
            request=request,
            reason_code=DenyReason.DORMANT,
            detail=f"action_type {request.action_type!r} is dormant",
        )

    # Gate 3: autonomy tier.
    autonomy = _autonomy_verdict(request.source, spec.base_tier)
    if autonomy == Verdict.AUTO_DENY:
        return _deny(
            request=request,
            reason_code=DenyReason.AUTONOMY_INSUFFICIENT,
            detail=(
                f"source {request.source.value} may not request "
                f"{spec.base_tier.name} actions"
            ),
        )
    if autonomy == Verdict.ESCALATE:
        return _escalate(
            request=request,
            spec=spec,
            reason_code=DenyReason.AUTONOMY_INSUFFICIENT,
            detail=(
                f"source {request.source.value} requests {spec.base_tier.name}; "
                "founder signature required"
            ),
            context=context,
        )

    # Gate 4: rate limit (if policy defined for this action).
    policy = context.rate_policy.get(request.action_type)
    if policy is not None and request.source_identity:
        limit, window = policy
        rl = context.active_rate_limiter()
        if not rl.allow(
            source_identity=request.source_identity,
            action_type=request.action_type,
            limit=limit,
            window_seconds=window,
        ):
            return _deny(
                request=request,
                reason_code=DenyReason.RATE_LIMITED,
                detail=f"rate limit exceeded: {limit}/{window}s",
            )

    # Gate 5: budget.
    if spec.max_cost_usd_cents is not None:
        if request.estimated_cost_usd_cents > spec.max_cost_usd_cents:
            return _escalate(
                request=request,
                spec=spec,
                reason_code=DenyReason.BUDGET_EXCEEDED,
                detail=(
                    f"estimated cost {request.estimated_cost_usd_cents}c "
                    f"exceeds action cap {spec.max_cost_usd_cents}c"
                ),
                context=context,
            )
    if (
        context.budget_remaining_usd_cents is not None
        and request.estimated_cost_usd_cents > context.budget_remaining_usd_cents
    ):
        return _deny(
            request=request,
            reason_code=DenyReason.BUDGET_EXCEEDED,
            detail=(
                f"estimated cost {request.estimated_cost_usd_cents}c "
                f"exceeds remaining budget {context.budget_remaining_usd_cents}c"
            ),
        )

    # Gate 6: required evidence kinds present.
    present_kinds = {c.kind for c in request.citations}
    missing = [k for k in spec.required_evidence_kinds if k not in present_kinds]
    if missing:
        return _escalate(
            request=request,
            spec=spec,
            reason_code=DenyReason.EVIDENCE_MISSING,
            detail=f"missing required evidence kinds: {missing}",
            context=context,
        )

    # Gate 7: hard constraints (tenant config blocklist).
    for constraint in context.hard_constraints:
        if constraint and constraint.lower() in request.action_type.lower():
            return _deny(
                request=request,
                reason_code=DenyReason.HARD_CONSTRAINT,
                detail=f"action matches hard constraint {constraint!r}",
            )
        for value in request.payload.values():
            if not isinstance(value, str):
                continue
            if constraint and constraint.lower() in value.lower():
                return _deny(
                    request=request,
                    reason_code=DenyReason.HARD_CONSTRAINT,
                    detail=f"payload matches hard constraint {constraint!r}",
                )

    # Gate 8: explicit approvals required for this action_type.
    if request.action_type in context.required_approvals:
        return _escalate(
            request=request,
            spec=spec,
            reason_code=DenyReason.AUTONOMY_INSUFFICIENT,
            detail=(
                f"action {request.action_type} requires an explicit signed "
                "approval before dispatch"
            ),
            context=context,
        )

    # Gate 9: trust-weighted tier floor.
    min_trust = context.min_trust_by_tier.get(spec.base_tier)
    if (
        min_trust is not None
        and context.trust_score_lookup is not None
        and request.source_identity
    ):
        score = context.trust_score_lookup(request.source_identity)
        if score < min_trust:
            return _deny(
                request=request,
                reason_code=DenyReason.TRUST_INSUFFICIENT,
                detail=(
                    f"trust score {score:.3f} below tier "
                    f"{spec.base_tier.name} floor {min_trust:.3f}"
                ),
            )

    return _approve(request)


__all__ = [
    "ActionRequest",
    "Citation",
    "DenyReason",
    "EscalationManifest",
    "EvaluatorContext",
    "EvaluatorDecision",
    "LocalDevSigner",
    "RateLimiter",
    "Signer",
    "Source",
    "Verdict",
    "evaluate",
]
