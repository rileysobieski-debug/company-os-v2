"""Tests for core.tenant_config.TenantConfig and friends."""
from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from core.tenant_config import (
    DelegationThresholds,
    HardConstraint,
    InheritedSystem,
    InheritedSystemKind,
    TenancyMode,
    TenantConfig,
    VerticalConfig,
    dump_tenant_config,
    load_tenant_config,
)


def _minimal_native() -> TenantConfig:
    return TenantConfig(
        slug="acme-co",
        display_name="Acme Co LLC",
        vertical_config=VerticalConfig(industry="b2b-saas"),
    )


# ---------------------------------------------------------------------------
# Basic shape
# ---------------------------------------------------------------------------
def test_native_defaults():
    cfg = _minimal_native()
    assert cfg.tenancy_mode == TenancyMode.NATIVE
    assert isinstance(cfg.tenant_id, UUID)
    assert cfg.inherited_systems == []
    cfg.assert_consistent()


def test_slug_rejects_separators():
    with pytest.raises(ValidationError):
        TenantConfig(
            slug="bad/slug",
            display_name="x",
            vertical_config=VerticalConfig(industry="x"),
        )
    with pytest.raises(ValidationError):
        TenantConfig(
            slug="..",
            display_name="x",
            vertical_config=VerticalConfig(industry="x"),
        )


def test_slug_rejects_whitespace():
    with pytest.raises(ValidationError):
        TenantConfig(
            slug=" has-space",
            display_name="x",
            vertical_config=VerticalConfig(industry="x"),
        )


# ---------------------------------------------------------------------------
# Tenancy-mode invariants
# ---------------------------------------------------------------------------
def test_native_cannot_have_inherited_systems():
    cfg = TenantConfig(
        slug="acme",
        display_name="Acme",
        vertical_config=VerticalConfig(industry="x"),
        inherited_systems=[
            InheritedSystem(
                kind=InheritedSystemKind.NOTION,
                workspace_id="w1",
                credential_handle="kms://notion/w1",
                display_name="Acme Notion",
            ),
        ],
    )
    with pytest.raises(ValueError, match="NATIVE"):
        cfg.assert_consistent()


def test_transition_without_systems_raises():
    cfg = TenantConfig(
        slug="acme",
        display_name="Acme",
        tenancy_mode=TenancyMode.TRANSITION,
        vertical_config=VerticalConfig(industry="x"),
    )
    with pytest.raises(ValueError, match="TRANSITION"):
        cfg.assert_consistent()


def test_transition_with_systems_ok():
    cfg = TenantConfig(
        slug="acme",
        display_name="Acme",
        tenancy_mode=TenancyMode.TRANSITION,
        vertical_config=VerticalConfig(industry="x"),
        inherited_systems=[
            InheritedSystem(
                kind=InheritedSystemKind.QUICKBOOKS,
                workspace_id="realm-42",
                credential_handle="kms://quickbooks/realm-42",
                display_name="Acme QB",
            ),
        ],
    )
    cfg.assert_consistent()


def test_inherited_system_must_be_read_only():
    with pytest.raises(ValidationError, match="read_only"):
        InheritedSystem(
            kind=InheritedSystemKind.SLACK,
            workspace_id="T1",
            credential_handle="kms://slack/T1",
            display_name="x",
            read_only=False,
        )


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------
def test_roundtrip(tmp_path):
    cfg = TenantConfig(
        slug="acme-co",
        display_name="Acme Co LLC",
        tenancy_mode=TenancyMode.NATIVE,
        vertical_config=VerticalConfig(
            industry="natural-wine-dtc",
            regulatory_surface=["TTB", "PLCB"],
            terminology={"vendor": "vineyard"},
        ),
        active_departments=["finance", "marketing"],
        hard_constraints=[
            HardConstraint(
                name="no-alcohol-to-minors",
                statement="Absolute. No exceptions. Ever.",
                added_at="2026-04-22T10:00:00Z",
            ),
        ],
        delegation_thresholds=DelegationThresholds(autonomous_min_trust=0.4),
    )
    p = tmp_path / "acme-co" / "tenant.json"
    dump_tenant_config(cfg, p)
    loaded = load_tenant_config(p)
    assert loaded.slug == cfg.slug
    assert loaded.vertical_config.industry == "natural-wine-dtc"
    assert loaded.delegation_thresholds.autonomous_min_trust == pytest.approx(0.4)
    assert loaded.hard_constraints[0].name == "no-alcohol-to-minors"


def test_slug_mismatch_on_disk_rejected(tmp_path):
    cfg = _minimal_native()
    p = tmp_path / "wrong-folder" / "tenant.json"
    dump_tenant_config(cfg, p)
    with pytest.raises(ValueError, match="slug mismatch"):
        load_tenant_config(p)


def test_delegation_thresholds_bounds():
    with pytest.raises(ValidationError):
        DelegationThresholds(autonomous_min_trust=2.5)
    with pytest.raises(ValidationError):
        DelegationThresholds(dormancy_days=0)
