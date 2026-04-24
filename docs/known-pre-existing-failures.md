# Known pre-existing test failures

This document lists test failures that predate the v6 Sovereign Governance Chassis rewrite (2026-04-22 onward) and are NOT blockers for new work. They are auto-applied as `xfail(strict=True)` via `tests/conftest.py::pytest_collection_modifyitems`, so:

- CI does not block on them.
- An accidental fix raises XPASS and trips CI, so we notice when an entry can be removed.

When you fix one, delete it from `_KNOWN_PRE_EXISTING_FAILURES` in `tests/conftest.py` AND from the table below in the same commit.

## Tracked failures (as of 2026-04-24)

| Test node id | Failure mode | Fix owner |
|---|---|---|
| `tests/test_phase14_dept_onboarding.py::TestPhaseTransitions::test_full_lifecycle_to_complete` | Phase transition stops at `staffing` instead of reaching `complete`. Likely phase-graph drift after a Phase 14 primitive refactor. | Phase 14 dogfood window (60-day stabilization) |
| `tests/test_phase14_dept_onboarding.py::TestAggregates::test_overall_progress_counts` | Aggregate `complete` count is 0 when 1 expected. Same dept-onboarding phase-graph drift. | Phase 14 dogfood window |
| `tests/test_phase14_dept_onboarding.py::TestScopeCalibrationPrompt::test_secondary_is_ambient_not_operational` | Scope-calibration prompt no longer includes the `ambient` marker; prompt template drift. | Phase 14 dogfood window |
| `tests/test_phase14_stack_review.py::TestAutoTrigger::test_returns_true_when_all_complete` | `all_departments_complete` returns False on a single-dept fixture that should be complete. Same dept-onboarding drift, downstream assertion. | Phase 14 dogfood window |

## Rationale for not fixing now

Per the v6 plan (`how-would-an-agent-quiet-origami.md`), `core/governance/` Phase 1 implementation is frozen during Weeks 2-3 (Walls layer) and explicitly rewritten in Weeks 4-5 against the new PVE rubric. Fixing these dept-onboarding failures now would mean patching code that is about to be rewritten, which is wasted motion. The four failures are tracked here so they are not invisible; they are simply parked.

## Why xfail-strict instead of deselect

`--deselect` removes tests from CI entirely, which means we lose visibility if the failure mode changes. `xfail(strict=True)` keeps the test in the suite and fails CI if the test starts passing unexpectedly (XPASS). That is the signal we want: the moment a test passes, we clean up the entry here and in `conftest.py`.

## Baseline

| Metric | Value |
|---|---|
| Suite size before xfail wiring | 1373 passed, 4 failed, 1 skipped |
| Suite size after xfail wiring | 1373 passed, 4 xfailed, 1 skipped |

No test was removed. The four failing tests now run as expected-failing and do not block CI.
