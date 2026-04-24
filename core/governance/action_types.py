"""Canonical registry of governance-relevant action types.

The deterministic evaluator (`core.governance.evaluator`) refuses any
`ActionRequest` whose `action_type` is not in this registry. Adding a
new action type is intentional friction: the engine cannot reason
about policy for a type it does not know. Every entry declares:

    - The base risk tier (ROUTINE through BOARD).
    - The evidence kinds a citation must carry for this action to pass
      the Provenance gate.
    - The spend ceiling in USD cents; None means unbounded at this
      layer and must be bounded by the Budget gate instead.
    - The `dormancy` flag; dormant actions auto-deny regardless of
      autonomy level until the founder re-activates them.
    - The handler reference (dotted path). The handler itself lives
      elsewhere; this field is resolved lazily by the dispatcher.

Tiers line up with the v6 plan:

    ROUTINE   tier 1 : routine specialist work, auto-approve path
    TRUSTED   tier 2 : cross-dept dispatch, manager signoff
    ELEVATED  tier 3 : spend, hiring, external-facing writes
    BOARD     tier 4 : strategy pivots, founder signature required

The registry is process-global but swappable via `use_registry` (same
pattern the tenants module uses). Tests get a clean slate per-test.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterator


class ActionTier(Enum):
    ROUTINE = 1
    TRUSTED = 2
    ELEVATED = 3
    BOARD = 4


@dataclass(frozen=True)
class ActionTypeSpec:
    name: str
    base_tier: ActionTier
    required_evidence_kinds: tuple[str, ...]
    max_cost_usd_cents: int | None
    handler: str
    dormancy: bool = False
    notes: str = ""


class ActionTypeUnknown(KeyError):
    """Raised when an `ActionRequest` references a type not in the
    registry. The evaluator treats this as auto-deny; dispatch is
    never attempted for an unknown action."""


class ActionTypeAlreadyRegistered(ValueError):
    """Raised on an attempt to overwrite a registered action type
    without `overwrite=True`. Silent overwrite would let a new
    registration weaken the tier of an existing action; the guard
    forces the caller to acknowledge the change."""


class ActionTypeRegistry:
    def __init__(self) -> None:
        self._entries: dict[str, ActionTypeSpec] = {}
        self._lock = threading.Lock()

    def register(self, spec: ActionTypeSpec, *, overwrite: bool = False) -> None:
        with self._lock:
            if spec.name in self._entries and not overwrite:
                raise ActionTypeAlreadyRegistered(
                    f"{spec.name} is already registered; pass overwrite=True "
                    "to replace.",
                )
            self._entries[spec.name] = spec

    def get(self, name: str) -> ActionTypeSpec:
        try:
            return self._entries[name]
        except KeyError as exc:
            raise ActionTypeUnknown(name) from exc

    def has(self, name: str) -> bool:
        return name in self._entries

    def known(self) -> list[str]:
        return sorted(self._entries.keys())

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


# Module-level default registry with the v6 seed entries. Consumers
# can either use this directly via `get_default_registry()` or swap a
# test-local one via `use_registry`.
_registry_lock = threading.Lock()
_default_registry: ActionTypeRegistry | None = None


def _build_default_registry() -> ActionTypeRegistry:
    """Create and populate the v6-seed registry. Kept as a function so
    tests can build a fresh default without side effects."""
    reg = ActionTypeRegistry()
    for spec in _SEED_ENTRIES:
        reg.register(spec)
    return reg


_SEED_ENTRIES: tuple[ActionTypeSpec, ...] = (
    ActionTypeSpec(
        name="dispatch_manager",
        base_tier=ActionTier.TRUSTED,
        required_evidence_kinds=("handshake", "brief"),
        max_cost_usd_cents=500,
        handler="core.managers.base:dispatch_manager",
        notes="Routine manager dispatch; budget envelope per-run.",
    ),
    ActionTypeSpec(
        name="dispatch_specialist",
        base_tier=ActionTier.ROUTINE,
        required_evidence_kinds=("manager_memory",),
        max_cost_usd_cents=200,
        handler="core.managers.base:dispatch_specialist",
    ),
    ActionTypeSpec(
        name="convene_board",
        base_tier=ActionTier.ELEVATED,
        required_evidence_kinds=("thesis", "pre_reads"),
        max_cost_usd_cents=1500,
        handler="core.board:convene",
    ),
    ActionTypeSpec(
        name="founder_override",
        base_tier=ActionTier.BOARD,
        required_evidence_kinds=("signed_override", "override_justification"),
        max_cost_usd_cents=None,
        handler="core.governance.evaluator:record_founder_override",
        notes="Board-tier; requires KMS-signed override per v6 plan.",
    ),
    ActionTypeSpec(
        name="import_entities",
        base_tier=ActionTier.TRUSTED,
        required_evidence_kinds=("adapter_credentials",),
        max_cost_usd_cents=None,
        handler="core.import_adapters:run_import",
        notes="Transition-mode ingest from Notion/QuickBooks/Slack.",
    ),
    ActionTypeSpec(
        name="harden_inherited_context",
        base_tier=ActionTier.ELEVATED,
        required_evidence_kinds=("founder_review",),
        max_cost_usd_cents=None,
        handler="core.governance.memory:harden",
        notes=(
            "Flips hardened=0 -> 1 on an inherited_context row. "
            "Required because shadow rows do not feed the hard-constraint gate."
        ),
    ),
    ActionTypeSpec(
        name="spend_commit",
        base_tier=ActionTier.ELEVATED,
        required_evidence_kinds=("vendor_bid", "approval"),
        max_cost_usd_cents=50_000,
        handler="core.primitives.cost:commit_hold",
        notes="Spend commit places an escrow hold via the Settlement layer.",
    ),
    ActionTypeSpec(
        name="record_decision",
        base_tier=ActionTier.ROUTINE,
        required_evidence_kinds=("decision_body",),
        max_cost_usd_cents=0,
        handler="core.governance.storage:record_decision",
    ),
)


def get_default_registry() -> ActionTypeRegistry:
    global _default_registry
    with _registry_lock:
        if _default_registry is None:
            _default_registry = _build_default_registry()
        return _default_registry


@contextmanager
def use_registry(registry: ActionTypeRegistry) -> Iterator[ActionTypeRegistry]:
    """Swap the process-wide registry for a block. Tests pass a fresh
    registry here to avoid cross-test pollution."""
    global _default_registry
    with _registry_lock:
        prior = _default_registry
        _default_registry = registry
    try:
        yield registry
    finally:
        with _registry_lock:
            _default_registry = prior


__all__ = [
    "ActionTier",
    "ActionTypeAlreadyRegistered",
    "ActionTypeRegistry",
    "ActionTypeSpec",
    "ActionTypeUnknown",
    "get_default_registry",
    "use_registry",
]
