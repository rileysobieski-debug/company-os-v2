# Rollback Plan: Returning to Phase 1 at Any Point During the 12-Week Sprint

**Status:** LIVE (2026-04-22). Week 1 deliverable. The invariant: at any point in Weeks 2-12, we must be able to restore a working Phase 1 chassis within minutes.

## Core property

Phase 1 SQLite vaults are never modified by Phase 2 code. Every Phase 2 touch of legacy data goes through `core.legacy_bridge.LegacyBridge`, which opens SQLite with `mode=ro` URIs and exposes no write methods. This makes rollback a pointer change, not a data-recovery exercise.

## Rollback procedure

### Case A: abandon mid-sprint, return to Phase 1

1. Stop the Phase 2 webapp process:
   ```bash
   pkill -f "python webapp/app.py --prod"
   ```
2. Check out the last Phase 1 commit:
   ```bash
   cd "/c/Users/riley_edejtwi/Obsidian Vault/company-os"
   git checkout 9f9676f   # Phase 1 frozen commit: chore: apply Apache 2.0 license
   ```
   (Or the pre-rewrite HEAD of `main` at the time rollback is initiated.)
3. Confirm `COMPANY_OS_VAULT_DIR` still points at the Phase 1 vault:
   ```bash
   echo "$COMPANY_OS_VAULT_DIR"
   # Expected: C:/Users/riley_edejtwi/Obsidian Vault
   ```
4. Restart the webapp:
   ```bash
   COMPANY_OS_VAULT_DIR="C:/Users/riley_edejtwi/Obsidian Vault" \
     nohup /c/Python314/python.exe webapp/app.py --host 127.0.0.1 --port 5050 --prod \
     > /tmp/webapp.log 2>&1 &
   ```
5. Smoke-check: `curl -fsS http://127.0.0.1:5050/c/Old%20Press%20Wine%20Company%20LLC/governance >/dev/null && echo OK`.

Total time: ~2 minutes. No data loss. Old Press continues to run against Phase 1 exactly as it did on 2026-04-21.

### Case B: partial rollback (one layer)

If a specific Phase 2 layer (Brain, Memory, Walls, Lens) is failing but the others are fine, revert only that layer's commits:

1. Identify the layer's commit range: `git log --oneline --grep='^Week [2-3]:'` for Walls, `'^Week [4-5]:'` for Brain+Memory, etc.
2. `git revert <hash>...<hash>` to create a revert commit rather than discarding history.
3. Re-run the sprint's CI suite. Pre-kernel harnesses MUST still xfail strict; if they pass unexpectedly, a partial rollback left an inconsistent state and full Case A rollback is required.

### Case C: vault-level corruption

If a Phase 2 bug has written garbage into the Postgres schema-per-tenant layout (not Phase 1), drop and recreate the affected schemas:

1. `psql` into the managed Postgres instance.
2. `DROP SCHEMA tenant_<uuid> CASCADE;`
3. Re-provision via `core.tenants.provision_tenant(slug)`.
4. Re-ingest from Legacy Bridge if the tenant had Phase 1 history.

Phase 1 SQLite files are unaffected because the bug is downstream of them.

## What is NOT a rollback

- **Not a rollback:** deleting the Postgres managed instance. That destroys Phase 2 data but does not help if the goal is to restore Phase 1 behavior; Phase 1 never used Postgres.
- **Not a rollback:** force-pushing over main. Force-push destroys the audit trail. Always use `git revert`.
- **Not a rollback:** editing Phase 1 SQLite files to "fix" data. They are read-only by contract; any write invalidates the rollback guarantee.

## Drill schedule

Per the risk register R6: run at least one rollback drill per 4-week block.

- **Week 4 drill:** execute Case A in a worktree (not on main), measure time-to-restore, verify smoke passes, note delta.
- **Week 8 drill:** execute Case B partial rollback of the Brain layer, verify the chassis still serves with just Memory/Walls/Lens active.
- **Week 12 drill:** before Phase 2 declared done, full Case A drill one more time to confirm the rollback property held all the way through.

## Commit boundaries we care about

- **Phase 1 frozen:** commit `9f9676f` (2026-04-21, Apache 2.0 license applied). Rollback target for Case A.
- **Phase 2 Week 1 scaffold:** commit `8126b10` (2026-04-22, rubric audit + architecture overview).
- **Phase 2 Week 1 immediate fixes:** commit `e3a6dcd` (2026-04-22, path-traversal + staleness + job_id helper).

Each subsequent week's cleanup commit is added here as it lands, so a future rollback can aim at a specific stable point.
