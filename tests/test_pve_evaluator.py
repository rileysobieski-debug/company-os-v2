"""Pure-Python evaluator (Brain layer) tests.

Covers the v6 governance evaluator at `core.governance.evaluator`.
Not to be confused with the existing Phase 7.2 `core.dispatch.evaluator`
which rates dispatch outcomes; the test file name uses "pve" to avoid
collision with `tests/test_evaluator.py`.

Each test pokes a single gate in the ordered stack.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from core.governance.action_types import (
    ActionTier,
    ActionTypeRegistry,
    ActionTypeSpec,
    use_registry,
)
from core.governance.evaluator import (
    ActionRequest,
    Citation,
    DenyReason,
    EvaluatorContext,
    LocalDevSigner,
    RateLimiter,
    Source,
    Verdict,
    evaluate,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _request(
    *,
    action_type: str = "dispatch_specialist",
    source: Source = Source.AGENT,
    payload: dict | None = None,
    citations: tuple[Citation, ...] | None = None,
    estimated_cost_usd_cents: int = 0,
    source_identity: str = "specialist:marketing.writer",
) -> ActionRequest:
    if citations is None:
        citations = (Citation(source_path="memory.md", content_hash="abc", kind="manager_memory"),)
    return ActionRequest(
        action_type=action_type,
        source=source,
        payload=payload or {},
        citations=citations,
        requested_at=_now(),
        estimated_cost_usd_cents=estimated_cost_usd_cents,
        source_identity=source_identity,
    )


@pytest.fixture
def registry() -> ActionTypeRegistry:
    reg = ActionTypeRegistry()
    reg.register(ActionTypeSpec(
        name="dispatch_specialist", base_tier=ActionTier.ROUTINE,
        required_evidence_kinds=("manager_memory",),
        max_cost_usd_cents=100, handler="h",
    ))
    reg.register(ActionTypeSpec(
        name="dispatch_manager", base_tier=ActionTier.TRUSTED,
        required_evidence_kinds=("brief",),
        max_cost_usd_cents=500, handler="h",
    ))
    reg.register(ActionTypeSpec(
        name="convene_board", base_tier=ActionTier.ELEVATED,
        required_evidence_kinds=("thesis",),
        max_cost_usd_cents=1500, handler="h",
    ))
    reg.register(ActionTypeSpec(
        name="founder_override", base_tier=ActionTier.BOARD,
        required_evidence_kinds=("signed_override",),
        max_cost_usd_cents=None, handler="h",
    ))
    reg.register(ActionTypeSpec(
        name="dormant_action", base_tier=ActionTier.ROUTINE,
        required_evidence_kinds=(), max_cost_usd_cents=None,
        handler="h", dormancy=True,
    ))
    return reg


@pytest.fixture
def ctx(registry):
    return EvaluatorContext(registry=registry)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------
def test_routine_agent_dispatch_approves(ctx) -> None:
    decision = evaluate(_request(), context=ctx)
    assert decision.verdict == Verdict.APPROVE


def test_approve_decision_carries_request_hash(ctx) -> None:
    req = _request()
    decision = evaluate(req, context=ctx)
    assert decision.request_hash == req.request_hash()


# ---------------------------------------------------------------------------
# Gate 1: unknown action type
# ---------------------------------------------------------------------------
def test_unknown_action_type_auto_denies(ctx) -> None:
    decision = evaluate(_request(action_type="not_registered"), context=ctx)
    assert decision.verdict == Verdict.AUTO_DENY
    assert decision.reason_code == DenyReason.UNKNOWN_ACTION


# ---------------------------------------------------------------------------
# Gate 2: dormancy
# ---------------------------------------------------------------------------
def test_dormant_action_auto_denies(ctx) -> None:
    req = _request(action_type="dormant_action", citations=())
    decision = evaluate(req, context=ctx)
    assert decision.verdict == Verdict.AUTO_DENY
    assert decision.reason_code == DenyReason.DORMANT


# ---------------------------------------------------------------------------
# Gate 3: autonomy
# ---------------------------------------------------------------------------
def test_agent_requesting_board_tier_is_denied(ctx) -> None:
    req = _request(
        action_type="founder_override",
        source=Source.AGENT,
        citations=(Citation(source_path="x", content_hash="h", kind="signed_override"),),
    )
    decision = evaluate(req, context=ctx)
    assert decision.verdict == Verdict.AUTO_DENY
    assert decision.reason_code == DenyReason.AUTONOMY_INSUFFICIENT


def test_agent_requesting_elevated_action_escalates(ctx) -> None:
    req = _request(
        action_type="convene_board",
        source=Source.AGENT,
        citations=(Citation(source_path="x", content_hash="h", kind="thesis"),),
    )
    decision = evaluate(req, context=ctx)
    assert decision.verdict == Verdict.ESCALATE
    assert decision.reason_code == DenyReason.AUTONOMY_INSUFFICIENT
    assert decision.manifest is not None
    assert "founder" in decision.manifest.authorized_principals


def test_founder_requesting_board_tier_approves(ctx) -> None:
    req = _request(
        action_type="founder_override",
        source=Source.FOUNDER,
        citations=(Citation(source_path="x", content_hash="h", kind="signed_override"),),
    )
    decision = evaluate(req, context=ctx)
    assert decision.verdict == Verdict.APPROVE


# ---------------------------------------------------------------------------
# Gate 4: rate limit
# ---------------------------------------------------------------------------
def test_rate_limiter_permits_within_budget() -> None:
    rl = RateLimiter()
    for _ in range(3):
        assert rl.allow(
            source_identity="s",
            action_type="a",
            limit=3,
            window_seconds=10,
        )
    assert not rl.allow(
        source_identity="s",
        action_type="a",
        limit=3,
        window_seconds=10,
    )


def test_evaluator_rate_limit_denies(registry) -> None:
    rate_limiter = RateLimiter()
    ctx = EvaluatorContext(
        registry=registry,
        rate_limiter=rate_limiter,
        rate_policy={"dispatch_specialist": (1, 60)},
    )
    first = evaluate(_request(), context=ctx)
    second = evaluate(_request(), context=ctx)
    assert first.verdict == Verdict.APPROVE
    assert second.verdict == Verdict.AUTO_DENY
    assert second.reason_code == DenyReason.RATE_LIMITED


# ---------------------------------------------------------------------------
# Gate 5: budget
# ---------------------------------------------------------------------------
def test_action_spec_cost_cap_escalates(ctx) -> None:
    req = _request(estimated_cost_usd_cents=500)
    decision = evaluate(req, context=ctx)
    assert decision.verdict == Verdict.ESCALATE
    assert decision.reason_code == DenyReason.BUDGET_EXCEEDED


def test_remaining_budget_exhausted_denies(registry) -> None:
    ctx = EvaluatorContext(registry=registry, budget_remaining_usd_cents=50)
    decision = evaluate(_request(estimated_cost_usd_cents=75), context=ctx)
    assert decision.verdict == Verdict.AUTO_DENY
    assert decision.reason_code == DenyReason.BUDGET_EXCEEDED


# ---------------------------------------------------------------------------
# Gate 6: evidence kinds
# ---------------------------------------------------------------------------
def test_missing_required_evidence_escalates(ctx) -> None:
    req = _request(citations=())
    decision = evaluate(req, context=ctx)
    assert decision.verdict == Verdict.ESCALATE
    assert decision.reason_code == DenyReason.EVIDENCE_MISSING
    assert decision.manifest is not None
    assert "manager_memory" in decision.manifest.required_evidence_kinds


# ---------------------------------------------------------------------------
# Gate 7: hard constraints
# ---------------------------------------------------------------------------
def test_hard_constraint_match_in_action_type_denies(registry) -> None:
    ctx = EvaluatorContext(
        registry=registry,
        hard_constraints=("dispatch_specialist",),
    )
    decision = evaluate(_request(), context=ctx)
    assert decision.verdict == Verdict.AUTO_DENY
    assert decision.reason_code == DenyReason.HARD_CONSTRAINT


def test_hard_constraint_match_in_payload_denies(registry) -> None:
    ctx = EvaluatorContext(
        registry=registry,
        hard_constraints=("crypto",),
    )
    req = _request(payload={"topic": "Investigate crypto partnerships"})
    decision = evaluate(req, context=ctx)
    assert decision.verdict == Verdict.AUTO_DENY
    assert decision.reason_code == DenyReason.HARD_CONSTRAINT


# ---------------------------------------------------------------------------
# Gate 8: explicit approvals
# ---------------------------------------------------------------------------
def test_explicit_approval_escalates(registry) -> None:
    ctx = EvaluatorContext(
        registry=registry,
        required_approvals=frozenset({"dispatch_specialist"}),
    )
    decision = evaluate(_request(), context=ctx)
    assert decision.verdict == Verdict.ESCALATE


# ---------------------------------------------------------------------------
# Gate 9: trust floor
# ---------------------------------------------------------------------------
def test_trust_below_tier_floor_denies(registry) -> None:
    ctx = EvaluatorContext(
        registry=registry,
        trust_score_lookup=lambda _agent: 0.1,
        min_trust_by_tier={ActionTier.ROUTINE: 0.5},
    )
    decision = evaluate(_request(), context=ctx)
    assert decision.verdict == Verdict.AUTO_DENY
    assert decision.reason_code == DenyReason.TRUST_INSUFFICIENT


def test_trust_above_tier_floor_permits(registry) -> None:
    ctx = EvaluatorContext(
        registry=registry,
        trust_score_lookup=lambda _agent: 0.8,
        min_trust_by_tier={ActionTier.ROUTINE: 0.5},
    )
    decision = evaluate(_request(), context=ctx)
    assert decision.verdict == Verdict.APPROVE


# ---------------------------------------------------------------------------
# Signer hook
# ---------------------------------------------------------------------------
def test_signer_attaches_signature_to_manifest(registry) -> None:
    signer = LocalDevSigner(key=b"fixed-key-for-determinism")
    ctx = EvaluatorContext(registry=registry, signer=signer)
    decision = evaluate(
        _request(
            action_type="convene_board",
            source=Source.AGENT,
            citations=(Citation(source_path="x", content_hash="h", kind="thesis"),),
        ),
        context=ctx,
    )
    assert decision.verdict == Verdict.ESCALATE
    assert decision.manifest is not None
    assert decision.manifest.signature
    assert decision.manifest.signer_fingerprint == signer.fingerprint()


def test_manifest_request_hash_stable() -> None:
    req = _request()
    h1 = req.request_hash()
    h2 = req.request_hash()
    assert h1 == h2 and len(h1) == 64


def test_request_hash_changes_with_payload() -> None:
    a = _request(payload={"x": 1})
    b = _request(payload={"x": 2})
    assert a.request_hash() != b.request_hash()


# ---------------------------------------------------------------------------
# Gate ordering
# ---------------------------------------------------------------------------
def test_unknown_action_short_circuits_budget_check(ctx) -> None:
    req = _request(action_type="nope", estimated_cost_usd_cents=10_000_000)
    decision = evaluate(req, context=ctx)
    assert decision.reason_code == DenyReason.UNKNOWN_ACTION


# ---------------------------------------------------------------------------
# Default registry fallback
# ---------------------------------------------------------------------------
def test_evaluator_uses_default_registry_when_none_provided(registry) -> None:
    with use_registry(registry):
        ctx = EvaluatorContext()
        decision = evaluate(_request(), context=ctx)
        assert decision.verdict == Verdict.APPROVE
