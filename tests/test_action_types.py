"""ActionTypeRegistry tests."""
from __future__ import annotations

import pytest

from core.governance.action_types import (
    ActionTier,
    ActionTypeAlreadyRegistered,
    ActionTypeRegistry,
    ActionTypeSpec,
    ActionTypeUnknown,
    get_default_registry,
    use_registry,
)


def test_registry_register_and_get() -> None:
    reg = ActionTypeRegistry()
    spec = ActionTypeSpec(
        name="test_action",
        base_tier=ActionTier.ROUTINE,
        required_evidence_kinds=("brief",),
        max_cost_usd_cents=0,
        handler="test:handler",
    )
    reg.register(spec)
    assert reg.get("test_action") is spec
    assert reg.has("test_action")


def test_registry_get_unknown_raises() -> None:
    reg = ActionTypeRegistry()
    with pytest.raises(ActionTypeUnknown):
        reg.get("not_registered")


def test_registry_duplicate_without_overwrite_raises() -> None:
    reg = ActionTypeRegistry()
    spec = ActionTypeSpec(
        name="dup",
        base_tier=ActionTier.ROUTINE,
        required_evidence_kinds=(),
        max_cost_usd_cents=None,
        handler="x",
    )
    reg.register(spec)
    with pytest.raises(ActionTypeAlreadyRegistered):
        reg.register(spec)


def test_registry_overwrite_flag_permits_replace() -> None:
    reg = ActionTypeRegistry()
    first = ActionTypeSpec(
        name="dup", base_tier=ActionTier.ROUTINE,
        required_evidence_kinds=(), max_cost_usd_cents=None, handler="x",
    )
    second = ActionTypeSpec(
        name="dup", base_tier=ActionTier.ELEVATED,
        required_evidence_kinds=(), max_cost_usd_cents=None, handler="y",
    )
    reg.register(first)
    reg.register(second, overwrite=True)
    assert reg.get("dup").handler == "y"
    assert reg.get("dup").base_tier == ActionTier.ELEVATED


def test_registry_known_is_sorted() -> None:
    reg = ActionTypeRegistry()
    for name in ("zed", "alpha", "middle"):
        reg.register(ActionTypeSpec(
            name=name, base_tier=ActionTier.ROUTINE,
            required_evidence_kinds=(), max_cost_usd_cents=None, handler="x",
        ))
    assert reg.known() == ["alpha", "middle", "zed"]


def test_default_registry_has_seed_entries() -> None:
    reg = get_default_registry()
    expected = {
        "dispatch_manager", "dispatch_specialist", "convene_board",
        "founder_override", "import_entities", "harden_inherited_context",
        "spend_commit", "record_decision",
    }
    assert expected.issubset(set(reg.known()))


def test_default_registry_founder_override_is_board_tier() -> None:
    reg = get_default_registry()
    spec = reg.get("founder_override")
    assert spec.base_tier == ActionTier.BOARD
    assert "signed_override" in spec.required_evidence_kinds


def test_default_registry_harden_requires_founder_review() -> None:
    reg = get_default_registry()
    spec = reg.get("harden_inherited_context")
    assert "founder_review" in spec.required_evidence_kinds
    assert spec.base_tier == ActionTier.ELEVATED


def test_use_registry_swaps_then_restores() -> None:
    swap = ActionTypeRegistry()
    swap.register(ActionTypeSpec(
        name="only_in_swap", base_tier=ActionTier.ROUTINE,
        required_evidence_kinds=(), max_cost_usd_cents=None, handler="x",
    ))
    original = get_default_registry()
    with use_registry(swap):
        assert get_default_registry() is swap
        assert get_default_registry().has("only_in_swap")
    assert get_default_registry() is original
    assert not original.has("only_in_swap")


def test_action_type_spec_is_frozen() -> None:
    spec = ActionTypeSpec(
        name="x", base_tier=ActionTier.ROUTINE,
        required_evidence_kinds=(), max_cost_usd_cents=None, handler="h",
    )
    with pytest.raises(Exception):
        spec.name = "mutated"  # type: ignore[misc]
