"""Tests for core.governance.citation — the v6 plan's reviewer-flagged
Week 1 blocker. If `test_ast_canonicalization_invariance` fails here,
Week 2 kernel work does not start.

Covers:

  - `hash_intent` returns identical SHA-256 for two versions of a
    Pydantic ActionRequest handler that differ only in whitespace,
    docstrings, and import ordering.
  - Logic-bearing edits DO change the hash (sanity: the canonicalizer
    must not be so lossy it approves real diffs).
  - Citation.verify_against round-trips.
  - Unknown canonicalizer versions raise cleanly.
"""
from __future__ import annotations

import pytest

from core.governance.citation import (
    CANONICALIZER_VERSION,
    Citation,
    canonicalize_source,
    hash_bytes,
    hash_intent,
)


VERSION_A = '''"""Handler module (Version A, standard layout)."""

from __future__ import annotations

from pydantic import BaseModel


class ActionRequest(BaseModel):
    """Validated intent from the Semantic Gateway."""

    action_type: str
    agent_id: str
    payload: dict


def handle(request: ActionRequest) -> str:
    """Apply the evaluator and return an outcome string."""
    outcome = "approved"
    if request.action_type == "finance.commit":
        outcome = "escalate"
    return outcome
'''


# Version B: same program intent, different cosmetic form:
#   - Module docstring removed.
#   - Function and class docstrings removed.
#   - Imports reordered.
#   - Blank lines added.
#   - Trailing whitespace on multiple lines.
#   - Inline comments added (comments never reach the AST).
#   - Function body identical (same variable names, same branches).
VERSION_B = """from pydantic import BaseModel
from __future__ import annotations


# class stub
class ActionRequest(BaseModel):
    action_type: str
    agent_id: str
    payload: dict



def handle(request: ActionRequest) -> str:
    outcome = "approved"   \n    if request.action_type == "finance.commit":
        outcome = "escalate"   # branch taken on commits
    return outcome
"""


def test_ast_canonicalization_invariance():
    """Reviewer-flagged Week 1 blocker: two cosmetically different
    versions of the same handler must produce identical hash_intent()
    output. Failure means the multi-version canonicalizer logic is
    broken before Brain work starts; stop and fix before proceeding.
    """
    h_a = hash_intent(VERSION_A)
    h_b = hash_intent(VERSION_B)
    assert h_a == h_b, (
        f"canonicalizer failed to produce identical hashes\n"
        f"  Version A: {h_a}\n"
        f"  Version B: {h_b}\n"
        f"  canonical A: {canonicalize_source(VERSION_A)[:200]}\n"
        f"  canonical B: {canonicalize_source(VERSION_B)[:200]}\n"
    )


def test_logic_bearing_edit_changes_hash():
    """Sanity: if the canonicalizer matches different program logic as
    equal, it is too lossy. A flipped comparison MUST produce a
    different hash."""
    version_c = VERSION_A.replace('"finance.commit"', '"finance.refund"')
    assert hash_intent(VERSION_A) != hash_intent(version_c)


def test_added_branch_changes_hash():
    version_d = VERSION_A.replace(
        '    return outcome\n',
        '    if request.agent_id == "founder":\n        return "approved"\n    return outcome\n',
    )
    assert hash_intent(VERSION_A) != hash_intent(version_d)


def test_citation_roundtrip():
    h = hash_intent(VERSION_A)
    cit = Citation(
        source_path="core/governance/evaluator.py",
        source_hash=h,
        annotation="pure-python evaluator handler",
    )
    assert cit.canonicalizer_version == CANONICALIZER_VERSION
    assert cit.verify_against(VERSION_A) is True
    assert cit.verify_against(VERSION_B) is True
    assert cit.verify_against(VERSION_A.replace("approved", "denied")) is False


def test_unknown_canonicalizer_version_raises():
    with pytest.raises(ValueError, match="canonicalizer version"):
        canonicalize_source("x = 1\n", version="v999")


def test_hash_bytes_separate_from_hash_intent():
    """Byte-hash and intent-hash are different functions; a fixture
    caller must not accidentally mix them. Simple check that the
    values differ for the same input."""
    src = "x = 1\n"
    assert hash_intent(src) != hash_bytes(src.encode("utf-8"))
