# Legacy Bridge: Read-Only Access to Phase 1 Data

**Status:** SCAFFOLD (2026-04-22). Week 1 deliverable. Replaces the destructive-migration approach flagged by Gemini's v2 review.

## Problem

Phase 1 shipped with one SQLite database per company under `<company>/governance/governance.sqlite`. Phase 2 moves governance state to schema-per-tenant Postgres. The naive migration (copy rows out of SQLite into Postgres, then delete the SQLite files) has two problems:

1. **Backfill-or-discard dilemma.** Decision rows written under Phase 1 did not carry citation hashes. A migration either fabricates hashes after the fact (which forges provenance) or discards the rows (which breaks the audit trail).
2. **Rollback risk.** If Week 2-12 work uncovers a blocker and we need to return to Phase 1, a destructive migration has already erased our way back.

## Solution

The Phase 1 SQLite vaults stay on disk, **untouched**, as read-only "Proof of Intent" archives. A `LegacyBridge` class treats them as a secondary read-only data source. Every row returned from Legacy Bridge carries `legacy=True` so it is never confused with hardened-provenance rows from the Phase 2 Postgres schema.

## Contract

```python
from core.legacy_bridge import LegacyBridge

bridge = LegacyBridge(phase1_vault_root="C:/Users/.../Old Press Wine Company LLC")

decisions = bridge.decisions(agent_id="manager:finance", limit=50)
for row in decisions:
    assert row.legacy is True
    assert row.citation_hash is None  # Phase 1 rows do not carry hashes
```

- Writes are forbidden at every level. Every method is a read. Any `__setattr__` on the connection raises `LegacyReadOnly`.
- The SQLite files are opened with `mode=ro` URI.
- Returned rows are frozen dataclasses with an explicit `legacy: bool = True` field.
- A `LegacyBridge` instance exposes: `decisions()`, `trust_snapshots()`, `onboarding_artifacts()`, `cost_log_entries()`. These are the only Phase 1 surfaces the Phase 2 UI needs.

## UI treatment

The Lens layer renders legacy rows with a muted-gray background and a small "legacy" badge. The Citation Drawer cannot open a provenance chain for a legacy row; clicking one shows the source file path and a note: "Pre-Phase-2 record. Hash not recorded. Retained for historical reference."

## Evaluator treatment

The deterministic evaluator treats legacy rows as Shadow Context automatically. They contribute to ambient awareness but never to the hardened-fact set. The founder can promote a legacy row to hardened only by explicitly citing it in a new decision under Phase 2 and signing that decision through the normal path.

## Rollback property

Because Legacy Bridge never mutates Phase 1 files, we can roll back the entire Phase 2 rewrite at any point in Weeks 2-12 by:

1. Reverting `webapp/app.py` to the pre-rewrite commit.
2. Pointing `COMPANY_OS_VAULT_DIR` at the Phase 1 vault layout.
3. Stopping the Phase 2 webapp process.

The founder has a working Phase 1 chassis back within minutes, and all historical data is intact because we never touched it.

## File locations (to be created in Weeks 2-3)

- `core/legacy_bridge.py` — the class, `LegacyReadOnly` exception, row dataclasses.
- `tests/test_legacy_bridge.py` — confirms writes raise, reads work against a fixture Phase 1 vault, and returned rows carry `legacy=True`.

## Relationship to Import Adapters

Legacy Bridge reads Phase 1 *self-owned* SQLite vaults. Import Adapters (Week 2-3) read *third-party* legacy stacks (Notion, QuickBooks, Slack). Different contracts, different code paths, same invariant: read-only, rows tagged as non-hardened until the founder explicitly promotes them.
