# Wine-Specific Content Extraction Inventory

**Status:** SCAFFOLD (2026-04-22). Built during Week 1 so Phase 2 kernel work does not carry wine terminology into an industry-agnostic chassis. Each entry names a file, the wine-specific token, and its proposed replacement under `TenantConfig.vertical_config`.

**Rule:** nothing under `core/` may reference `wine`, `alcohol`, `vineyard`, `TTB`, `PLCB`, `winery`, `cola`, or equivalent industry terms. Every string like that is a `vertical_config.terminology` key or a regulatory-surface list entry keyed by the tenant.

## Extraction target fields on `TenantConfig.vertical_config`

- `industry` (str) — short slug, e.g. `natural-wine-dtc`, `b2b-saas`, `enterprise-healthcare`.
- `regulatory_surface` (list[str]) — bodies and frames, e.g. `["TTB", "PLCB", "FDA", "HIPAA"]`.
- `terminology` (dict[str, str]) — tenant-chosen keys mapping generic terms to industry-specific ones; prompts read via substitution.
- `prompt_adjustments` (dict[str, str]) — full-string overrides for named prompt templates when terminology substitution is not enough.

## Inventory (to be populated in Week 1 walk)

| File:line | Current token | Context | Replacement |
|---|---|---|---|
| (pending) | `wine` | scattered in `core/` comments and prompt strings | `vertical_config.terminology["product"]` |
| (pending) | `winery` / `wine company` | agent role descriptions | `vertical_config.terminology["producer"]` |
| (pending) | `vineyard` | agent role descriptions, tasting notes | `vertical_config.terminology["supplier"]` or `vertical_config.terminology["source_site"]` |
| (pending) | `TTB` | compliance prompts | `vertical_config.regulatory_surface` entry; prompt template reads the list |
| (pending) | `PLCB` | compliance prompts (PA) | `vertical_config.regulatory_surface` entry |
| (pending) | `COLA` | label-approval prompts | `vertical_config.prompt_adjustments["label_compliance"]` |
| (pending) | `alcohol` | onboarding seed prompts, scope files | `vertical_config.industry` string; prompts read that |

## Walk procedure

1. `grep -rni --include='*.py' -E '\b(wine|alcohol|vineyard|TTB|PLCB|winery|COLA)\b' core/` inside a worktree.
2. For each hit, decide whether the string is (a) domain-neutral and can be parameterized via `terminology`, or (b) template-level and needs a full `prompt_adjustments` override.
3. Write the finding row above.
4. Produce the minimal code change to read from `vertical_config` instead of the hard-coded string.
5. Verify: the Quarry Ridge fixture tenant still loads with `industry="natural-wine-dtc"` and its prompts render with the same words they do today.

## Non-goals

- This inventory is for `core/`. Fixture vault content (inside `fixtures/sample-vault/Quarry Ridge Wine Co. LLC/`) is wine-specific by design and stays as-is; that is the demonstration tenant.
- The Old Press live vault under `C:/Users/riley_edejtwi/Obsidian Vault/Old Press Wine Company LLC/` is also wine-specific by design; same reasoning.

## Completion gate

The inventory is complete when:
1. Every file under `core/` passes a grep for all seven tokens with zero hits.
2. The Quarry Ridge fixture still loads and its arrival-note prompts render unchanged.
3. A synthetic non-wine tenant (e.g., `industry="b2b-saas"`) can be provisioned and its prompts render without any wine terminology leaking in.
