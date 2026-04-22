# Risk Register: 12-Week Sovereign Governance Chassis Sprint

**Status:** LIVE (2026-04-22). Week 1 deliverable. Updated at the end of every week with new risks surfaced and closed.

Format: each risk has a probability (`low` / `med` / `high`), impact (`low` / `med` / `high` / `critical`), mitigation owner-week, and rollback plan.

---

## R1: Postgres connection exhaustion under schema-per-tenant load

- **Probability:** med
- **Impact:** high (tenant requests stall, cascade to timeouts)
- **Mitigation:** PgBouncer session-mode pooling wired in Week 2. Connection limits set per tenant. CI load test hits 100 concurrent writes across 10 tenants and verifies zero `database is locked` errors.
- **Rollback:** revert to Phase 1 SQLite vault layout (see `docs/rollback.md`).
- **Owner:** Week 2-3.

## R2: Hash invalidation on whitespace changes in prompts

- **Probability:** high if unmitigated, low with canonicalizer
- **Impact:** med (citation chain breaks spuriously, noisy false positives)
- **Mitigation:** `core.governance.citation.canonicalize_source` v1 strips whitespace, docstrings, and sorts imports before hashing. Reviewer-flagged `test_ast_canonicalization_invariance` gates Week 2. Passed 2026-04-22.
- **Rollback:** not applicable; canonicalizer is additive.
- **Owner:** Week 1 (shipped).

## R3: DLQ drain worker interferes with 14-day decay worker

- **Probability:** med
- **Impact:** high (governance writes lost or ambient context corrupted)
- **Mitigation:** workers run in separate processes with explicit file-level locks on the DLQ journal. Decay worker reads only the `hardened` and `shadow` tables, never the DLQ. CI test launches both workers concurrently against a synthetic backlog and verifies no dropped records.
- **Rollback:** disable decay worker, drain DLQ manually, re-enable once fix lands.
- **Owner:** Week 4-5.

## R4: Coinbase testnet policy changes between plan and Week 7

- **Probability:** low
- **Impact:** med (x402 integration delayed; mocked path used instead)
- **Mitigation:** x402 settlement is explicitly mocked for Weeks 6-7 per v6. Mocked-success-bit verification proves the SLA-Escrow-Citation loop without real testnet dependency. Real Coinbase integration deferred to Phase 3.
- **Rollback:** stay on mock.
- **Owner:** Week 6-7.

## R5: Llama 4 output-shape parity with Anthropic

- **Probability:** med
- **Impact:** med (model-swap demo produces different structural outputs)
- **Mitigation:** MCP adapter normalizes output shape. `tests/model_swap_harness.py` asserts shape parity (field set, types). If parity breaks on a specific model, we either ship a model-specific adapter or document the limitation and exclude that model from the "under 5 minutes" claim.
- **Rollback:** default backend stays Anthropic; Llama 4 listed as experimental.
- **Owner:** Week 6-7.

## R6: Solo founder illness or interruption blocks the single-track sprint

- **Probability:** low-med
- **Impact:** critical
- **Mitigation:** 4 hrs/week reserved for Old Press operations is non-negotiable and absorbs small slips. Plan is elastic: if any week runs over, Lens (UI) complexity is cannibalized first, then Walls (physical Wasm deferred is already baked in), never Brain or Memory. Every commit is pushed to GitHub daily so work survives a laptop failure.
- **Rollback:** revert to Phase 1 layout at the current commit boundary.
- **Owner:** ongoing.

## R7: Transition-mode imports land as hard constraints and create non-determinism

- **Probability:** high if unmitigated, low with v6 fix
- **Impact:** critical (the deterministic engine's integrity fails)
- **Mitigation:** Gemini's v5 review flagged this. v6 fix: imports default to Shadow Context with `hardened=False`. Only hardened rows participate in PVE hard-constraint evaluation. Unhardened rows surface as UI Tension events awaiting founder signoff.
- **Rollback:** not applicable; the invariant is structural in Memory layer design.
- **Owner:** Week 4-5.

## R8: KMS outage blocks all EscalationManifest signing

- **Probability:** low
- **Impact:** high (no overrides can be signed during the outage)
- **Mitigation:** evaluator falls back to queue-and-retry for override requests; never forges a local signature. Queue has a 4-hour TTL. Founder is notified via Telegram (reuses `telegram-vault-bot`) if an override has been queued longer than 15 minutes.
- **Rollback:** use a secondary KMS region. Keys are mirrored at setup.
- **Owner:** Week 4-5.

## R9: Hypothesis / property tests surface a regression in Week 2-3

- **Probability:** med
- **Impact:** med (ships later)
- **Mitigation:** pre-kernel harness tests are wired as xfail-strict so unexpected pass flags a surprising implementation. Cross-tenant SQL injection probes cover seven attack shapes including `search_path` manipulation and orphaned session reuse.
- **Rollback:** not applicable; findings are fix-forward.
- **Owner:** Week 2-3.

## R10: Founder vault grows during sprint; migration tooling drifts

- **Probability:** med
- **Impact:** low (manual reconciliation on cut-over)
- **Mitigation:** Legacy Bridge is read-only. Old Press vault grows under Phase 1 during the sprint; Phase 2 reads it via the bridge once the cut-over happens. No migration required.
- **Rollback:** already in place by design.
- **Owner:** none (structural).

---

## Closed risks

(none yet; risks close at the end of the owner-week when verification passes)

## Weekly update procedure

End of each week:
1. Append any new risks surfaced by that week's work.
2. Update status of open risks: unchanged / closed / reclassified.
3. Note rollback drills run this week (target: at least one per 4-week block).
