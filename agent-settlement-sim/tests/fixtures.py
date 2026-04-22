"""
agent-settlement-sim/tests/fixtures.py -- Local evaluator test doubles.

Self-contained copy of StubPassthroughEvaluator so sim tests don't have a
hard import dependency on tests/fixtures/evaluators.py from the repo root.
"""
from __future__ import annotations

from core.primitives.evaluator import EvaluationOutput
from core.primitives.sla import InterOrgSLA


class StubPassthroughEvaluator:
    """Test double satisfying PrimaryEvaluator; returns canned output."""

    def __init__(
        self,
        evaluator_did: str,
        canonical_hash: str,
        canned_output: EvaluationOutput,
    ) -> None:
        self._evaluator_did = evaluator_did
        self._canonical_hash = canonical_hash
        self._canned_output = canned_output

    @property
    def evaluator_did(self) -> str:
        return self._evaluator_did

    @property
    def canonical_hash(self) -> str:
        return self._canonical_hash

    def evaluate(
        self,
        sla: InterOrgSLA,
        artifact_bytes: bytes,
        *,
        artifact_properties: dict | None = None,
    ) -> EvaluationOutput:
        return self._canned_output
