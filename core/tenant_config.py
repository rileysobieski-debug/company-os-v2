"""TenantConfig: structured per-tenant settings for the Phase 2 chassis.

Replaces the scattered `config.json`, hard-coded prompts, and
wine-specific comments in the Phase 1 codebase with a single Pydantic
model. Every new tenant (native, transition, or hybrid) loads into this
shape before the Brain / Memory / Walls / Lens layers touch it.

Design notes (v6 plan):

  - `tenancy_mode` gates the ingress path. `native` = greenfield,
    no import adapters. `transition` = existing business whose legacy
    stacks (Notion, QuickBooks, Slack) are read-only mirrored in as
    Shadow Context. `hybrid` = tenant that finished the transition wave
    and now runs mostly native but retains a few Legacy Bridge reads.
  - `vertical_config` is an escape hatch for industry-specific strings
    and regulatory surface (TTB for wine, HIPAA for healthcare, etc.).
    Nothing under `core/` should reference industry terms directly;
    everything routes through this dict.
  - `inherited_systems` is populated for `transition` tenants only. Each
    entry names a legacy system and carries a KMS-backed handle to the
    OAuth token or workspace id that the Import Adapter uses. Native
    tenants keep this empty.
  - `hard_constraints` and `settled_convictions` feed the deterministic
    evaluator. They are user-editable; the evaluator never rewrites
    them.
  - `delegation_thresholds` maps autonomy levels to numeric gates the
    evaluator reads at runtime.

This module has no side effects on import. Loading from disk is done
by `load_tenant_config(path)` which reads a JSON file and returns a
validated model.
"""
from __future__ import annotations

import json
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TenancyMode(str, Enum):
    """How a tenant reaches the chassis."""

    NATIVE = "native"
    TRANSITION = "transition"
    HYBRID = "hybrid"


class InheritedSystemKind(str, Enum):
    """Known import-adapter kinds. Open enum: new kinds can be added
    without breaking existing tenants, but every value the platform
    ships first-party code for lives here."""

    NOTION = "notion"
    QUICKBOOKS = "quickbooks"
    SLACK = "slack"
    GOOGLE_DRIVE = "google_drive"
    GMAIL = "gmail"
    STRIPE = "stripe"
    SHOPIFY = "shopify"
    OTHER = "other"


class InheritedSystem(BaseModel):
    """One legacy stack mirrored into the tenant. Transition-mode only.

    `credential_handle` is a reference to a KMS/Vault record, never
    the token itself. Format is left as a string so different KMS
    providers can plug in their own handle schemes.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: InheritedSystemKind
    workspace_id: str = Field(
        ...,
        description="Legacy-side identifier: Notion workspace id, QuickBooks realm id, Slack team id, etc.",
    )
    credential_handle: str = Field(
        ...,
        description="Opaque handle into the KMS/Vault record holding the OAuth token or API key. Never the token.",
    )
    display_name: str = Field(
        ...,
        description="Human-readable label for the UI. Not trusted for routing.",
    )
    read_only: bool = Field(
        default=True,
        description="Import Adapters are read-only by contract; this field exists so the schema can assert it.",
    )

    @field_validator("read_only")
    @classmethod
    def _must_be_read_only(cls, v: bool) -> bool:
        # The Import Adapter framework (Weeks 2-3) must refuse writes.
        # The schema enforces the contract at the data layer too.
        if not v:
            raise ValueError("InheritedSystem.read_only must be True; writes are a chassis violation.")
        return v


class DelegationThresholds(BaseModel):
    """Numeric gates the deterministic evaluator reads to decide when an
    agent may act autonomously vs. escalate to the founder. All values
    are in the -1..+1 trust-score range."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    autonomous_min_trust: float = Field(
        default=0.35,
        ge=-1.0,
        le=1.0,
        description="Below this trust score the agent cannot act without explicit approval.",
    )
    dormancy_days: int = Field(
        default=60,
        ge=1,
        description="After N days without a rating sample the agent is dormant and requires re-approval on any non-trivial action.",
    )
    high_risk_requires_founder: bool = Field(
        default=True,
        description="High-risk action types (finance, public comms) always route to founder regardless of trust.",
    )


class HardConstraint(BaseModel):
    """A founder-authored non-negotiable. The evaluator rejects any
    ActionRequest that would violate one of these; the only path past
    a HardConstraint is an EscalationManifest signed via KMS."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    statement: str
    added_at: str
    added_by: str = Field(default="founder")


class VerticalConfig(BaseModel):
    """Industry-specific glue. Every string under `core/` that used to
    say 'wine' or 'TTB' now reads from here so the chassis stays
    industry-agnostic at the module level."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    industry: str = Field(..., description="Short industry label, e.g. 'natural-wine-dtc', 'b2b-saas'.")
    regulatory_surface: list[str] = Field(
        default_factory=list,
        description="Regulatory bodies and frames this tenant must account for: TTB, PLCB, HIPAA, SOC2, etc.",
    )
    terminology: dict[str, str] = Field(
        default_factory=dict,
        description="Industry-specific terminology injected into prompts. Keys are tenant-chosen.",
    )
    prompt_adjustments: dict[str, str] = Field(
        default_factory=dict,
        description="Optional per-prompt-template string overrides keyed by template id.",
    )


class TenantConfig(BaseModel):
    """Top-level per-tenant configuration.

    `tenant_id` is authoritative. `slug` is the human-readable
    identifier used in URLs and filesystem paths; it must be stable
    and URL-safe. Mismatches between the on-disk folder name and
    `slug` fail loudly at load time.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: UUID = Field(default_factory=uuid4)
    slug: str = Field(..., min_length=1)
    display_name: str

    tenancy_mode: TenancyMode = TenancyMode.NATIVE

    vertical_config: VerticalConfig

    secondary_pool_extensions: list[str] = Field(
        default_factory=list,
        description="Candidate-slate secondary expertise labels the founder wants available for this tenant.",
    )
    active_departments: list[str] = Field(
        default_factory=list,
        description="Dept slugs the founder has actually staffed, in order.",
    )

    delegation_thresholds: DelegationThresholds = Field(default_factory=DelegationThresholds)
    hard_constraints: list[HardConstraint] = Field(default_factory=list)
    settled_convictions: list[str] = Field(
        default_factory=list,
        description="Founder-authored long-form convictions. Advisory to the evaluator; not binding the way hard_constraints are.",
    )

    inherited_systems: list[InheritedSystem] = Field(
        default_factory=list,
        description="Transition-mode only. Must be empty for NATIVE tenants; the loader enforces this.",
    )

    @field_validator("slug")
    @classmethod
    def _slug_shape(cls, v: str) -> str:
        bad = {"/", "\\", "..", "\x00"}
        for token in bad:
            if token in v:
                raise ValueError(f"slug {v!r} contains disallowed token {token!r}")
        if v.strip() != v or not v:
            raise ValueError("slug must be non-empty and have no leading/trailing whitespace")
        return v

    def assert_consistent(self) -> None:
        """Cross-field invariants Pydantic cannot express declaratively."""
        if self.tenancy_mode == TenancyMode.NATIVE and self.inherited_systems:
            raise ValueError(
                "NATIVE tenants cannot have inherited_systems. "
                "Set tenancy_mode = TRANSITION or HYBRID to import legacy stacks.",
            )
        if self.tenancy_mode == TenancyMode.TRANSITION and not self.inherited_systems:
            # Warning-level; not every TRANSITION tenant has imports wired
            # yet. We raise to surface the ambiguity; callers that want
            # to defer can catch and proceed.
            raise ValueError(
                "TRANSITION tenants must declare at least one inherited_system, "
                "or start as NATIVE and convert once imports are wired.",
            )


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------
def load_tenant_config(path: str | Path) -> TenantConfig:
    """Load and validate a TenantConfig from a JSON file.

    The file's parent directory name must match `slug`. Mismatch is a
    hard error so we cannot silently mis-resolve a tenant at runtime.
    """
    p = Path(path)
    raw = json.loads(p.read_text(encoding="utf-8"))
    cfg = TenantConfig.model_validate(raw)
    parent = p.parent.name
    if parent and parent != cfg.slug:
        raise ValueError(
            f"tenant_config slug mismatch: file lives under {parent!r} "
            f"but slug is {cfg.slug!r}",
        )
    cfg.assert_consistent()
    return cfg


def dump_tenant_config(cfg: TenantConfig, path: str | Path) -> None:
    """Serialize a TenantConfig to disk as pretty JSON. `tenant_id` is
    serialized as a string so the JSON stays human-readable."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = json.loads(cfg.model_dump_json())
    p.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
