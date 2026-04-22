"""
agent-settlement-sim/researcher_loop.py -- Settlement scenario simulator
========================================================================
A state-machine simulator for testing the full oracle settlement lifecycle.
States: created -> verifying -> settling -> done.

V1b extensions (B5):
  - `handle_verifying`: dispatches to Tier 0 or Tier 1 based on
    `ctx.sla.primary_evaluator_did`. Tier 1 uses `self.evaluator`
    (default: `StubPassthroughEvaluator`).
  - `handle_settling`: passes `now`, `challenge_window_sec`,
    `expected_primary_evaluator_did`, and `expected_evaluator_canonical_hash`
    to `release_pending_verdict`.
  - `fast_forward(seconds)`: advances the sim's internal clock so tests
    can skip past the challenge window without sleeping.

Usage (minimal):
    sim = ResearcherSim(adapter=mock_adapter, oracle=oracle)
    ctx = ScenarioCtx(sla=sla, artifact_bytes=b"...", handle=handle)
    sim.run(ctx)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from core.primitives.oracle import Oracle, OracleVerdict
from core.primitives.sla import InterOrgSLA


# ---------------------------------------------------------------------------
# ScenarioCtx: carries all per-run mutable state
# ---------------------------------------------------------------------------
@dataclass
class ScenarioCtx:
    """Per-scenario context threaded through state handlers.

    Attributes
    ----------
    sla:
        The InterOrgSLA governing this scenario. Must have
        `artifact_hash_at_delivery` populated before `run()` is called.
    artifact_bytes:
        Raw bytes of the artifact the provider delivered.
    handle:
        EscrowHandle returned from a prior `adapter.lock(...)` call.
    verdict:
        Populated by `handle_verifying`; consumed by `handle_settling`.
    state:
        Current state machine state. Transitions: created -> verifying
        -> settling -> done.
    """

    sla: InterOrgSLA
    artifact_bytes: bytes
    handle: Any  # EscrowHandle
    verdict: OracleVerdict | None = None
    state: str = "created"

    # Optional override verdict for the challenge path (Tier 3 supersede).
    override_verdict: OracleVerdict | None = None


# ---------------------------------------------------------------------------
# ResearcherSim: the state machine
# ---------------------------------------------------------------------------
class ResearcherSim:
    """Settlement scenario simulator.

    Attributes
    ----------
    adapter:
        A `MockSettlementAdapter` (or any compatible adapter) used for
        escrow operations and event recording.
    oracle:
        An `Oracle` instance used to issue Tier 0 and Tier 1 verdicts.
    evaluator:
        A `PrimaryEvaluator` instance used when the SLA nominates a
        primary evaluator. Defaults to a `StubPassthroughEvaluator`
        that returns `accepted` with score 0.95.
    _clock:
        Internal sim clock. `None` until set; when `None`, `_now()`
        returns `datetime.now(timezone.utc)`. Updated by `fast_forward`.
    """

    def __init__(
        self,
        *,
        adapter: Any,
        oracle: Oracle,
        evaluator: Any = None,
    ) -> None:
        self.adapter = adapter
        self.oracle = oracle
        self._clock: datetime | None = None

        if evaluator is None:
            # Lazy import: use the sim's own fixture stub so there is no
            # dependency on the repo-root tests/ package at runtime.
            from agent_settlement_sim.tests.fixtures import StubPassthroughEvaluator
            from core.primitives.evaluator import EvaluationOutput

            _DEFAULT_DID = "did:companyos:sim-default-evaluator"
            _DEFAULT_HASH = "sim-default-evaluator-v1b"
            _output = EvaluationOutput(
                result="accepted",
                score=Decimal("0.95"),
                evidence={"kind": "schema_pass_with_score"},
                evaluator_canonical_hash=_DEFAULT_HASH,
            )
            self.evaluator = StubPassthroughEvaluator(
                evaluator_did=_DEFAULT_DID,
                canonical_hash=_DEFAULT_HASH,
                canned_output=_output,
            )
        else:
            self.evaluator = evaluator

    # ------------------------------------------------------------------
    # Clock management
    # ------------------------------------------------------------------
    def _now(self) -> datetime:
        """Return the current sim clock time.

        If `fast_forward` has been called, returns the advanced clock.
        Otherwise returns `datetime.now(timezone.utc)` (wall-clock).
        """
        if self._clock is None:
            return datetime.now(timezone.utc)
        return self._clock

    def fast_forward(self, seconds: int | float) -> None:
        """Advance the sim's internal clock by `seconds`.

        The first call initialises the clock to `datetime.now(timezone.utc)`
        before adding the delta, so subsequent calls accumulate.

        Parameters
        ----------
        seconds:
            Number of seconds to advance. Negative values are rejected.

        Raises
        ------
        ValueError:
            If `seconds` is negative.
        """
        if seconds < 0:
            raise ValueError(
                f"fast_forward requires non-negative seconds, got {seconds!r}"
            )
        if self._clock is None:
            self._clock = datetime.now(timezone.utc)
        self._clock = self._clock + timedelta(seconds=seconds)

    # ------------------------------------------------------------------
    # State handlers
    # ------------------------------------------------------------------
    def handle_verifying(self, ctx: ScenarioCtx) -> None:
        """Issue a Tier 0 or Tier 1 verdict depending on the SLA.

        Dispatches to Tier 1 when `ctx.sla.primary_evaluator_did` is set
        (non-empty string). Otherwise dispatches to Tier 0.

        For Tier 1, uses `self.evaluator`. The SLA's `canonical_evaluator_hash`
        is checked inside `Oracle.evaluate_tier1` before the evaluator is called.

        Side effect: populates `ctx.verdict`.
        """
        if ctx.sla.primary_evaluator_did:
            # Tier 1: named primary evaluator.
            ctx.verdict = self.oracle.evaluate_tier1(
                ctx.sla,
                ctx.artifact_bytes,
                evaluator=self.evaluator,
            )
        else:
            # Tier 0: deterministic schema verification.
            ctx.verdict = self.oracle.evaluate_tier0(ctx.sla, ctx.artifact_bytes)

        ctx.state = "settling"

    def handle_settling(self, ctx: ScenarioCtx) -> None:
        """Settle the escrow against the current verdict.

        If `ctx.override_verdict` is set, uses that instead of `ctx.verdict`.
        Passes `now` (from the sim clock), `challenge_window_sec`, and
        evaluator authorization fields to `release_pending_verdict`.

        Side effect: advances `ctx.state` to "done".

        Returns
        -------
        SettlementReceipt
            The receipt from the adapter.
        """
        active_verdict = ctx.override_verdict if ctx.override_verdict is not None else ctx.verdict
        if active_verdict is None:
            raise RuntimeError("handle_settling called before handle_verifying")

        sla = ctx.sla

        # Build kwargs for challenge-window enforcement.
        settle_kwargs: dict = {
            "expected_artifact_hash": sla.artifact_hash_at_delivery,
            "requester_did": sla.requester_node_did,
            "provider_did": sla.provider_node_did,
            "now": self._now(),
            "challenge_window_sec": sla.challenge_window_sec,
        }

        # Evaluator authorization: pass expected DID + hash when set on SLA.
        # Skip the canonical hash check for timeout verdicts (evaluator never ran,
        # so no hash appears in evidence; the adapter would incorrectly block).
        is_timeout = (
            active_verdict.result == "refunded"
            and active_verdict.evidence.get("kind") == "evaluator_timeout"
        )
        if sla.primary_evaluator_did:
            settle_kwargs["expected_primary_evaluator_did"] = sla.primary_evaluator_did
        if sla.canonical_evaluator_hash and not is_timeout:
            settle_kwargs["expected_evaluator_canonical_hash"] = sla.canonical_evaluator_hash

        receipt = self.adapter.release_pending_verdict(
            ctx.handle,
            active_verdict,
            **settle_kwargs,
        )
        ctx.state = "done"
        return receipt

    # ------------------------------------------------------------------
    # Runner
    # ------------------------------------------------------------------
    def run(self, ctx: ScenarioCtx) -> None:
        """Drive `ctx` through the state machine from `created` to `done`.

        States: created -> verifying -> settling -> done.
        """
        ctx.state = "verifying"
        self.handle_verifying(ctx)
        self.handle_settling(ctx)
