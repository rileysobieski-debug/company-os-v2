"""
agent-settlement-sim/_default_evaluator.py -- Default evaluator for the sim.

Provides a `StubPassthroughEvaluator` pre-configured as the sim's default
when no explicit evaluator is passed to `ResearcherSim`. Isolated in its own
module so `researcher_loop.py` can lazy-import it without creating a hard dep
on tests/fixtures/ at module load time.
"""
from __future__ import annotations

from decimal import Decimal

from core.primitives.evaluator import EvaluationOutput

_DEFAULT_EVALUATOR_DID = "did:companyos:sim-default-evaluator"
_DEFAULT_CANONICAL_HASH = "sim-default-evaluator-v1b"


def _make_default_evaluator():
    """Return a `StubPassthroughEvaluator` configured for the sim default."""
    # Local import keeps this importable without tests/ on sys.path.
    try:
        from tests.fixtures.evaluators import StubPassthroughEvaluator
    except ModuleNotFoundError:
        from agent_settlement_sim._stub_evaluator import StubPassthroughEvaluator

    output = EvaluationOutput(
        result="accepted",
        score=Decimal("0.95"),
        evidence={"kind": "schema_pass_with_score"},
        evaluator_canonical_hash=_DEFAULT_CANONICAL_HASH,
    )
    return StubPassthroughEvaluator(
        evaluator_did=_DEFAULT_EVALUATOR_DID,
        canonical_hash=_DEFAULT_CANONICAL_HASH,
        canned_output=output,
    )
