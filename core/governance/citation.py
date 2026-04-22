"""Citation hashing with semantic canonicalization.

The Sovereign Governance Standard requires every state change to carry
a SHA-256 hash to its source artifact. A naive byte-hash breaks under
trivial edits (whitespace, docstring tweaks, reordered imports) that
do not change program intent. This module hashes a canonicalized form
of the source so only logic-bearing edits invalidate the chain.

Scope of canonicalization (v1):

  - Strips module / function / class / async-function docstrings.
  - Sorts top-level `import` and `from ... import` statements.
  - Normalizes line endings and trailing whitespace.
  - Parses to `ast` and dumps with field annotations, which elides
    whitespace and comments.

Not in scope for v1 (documented for Phase 4-5 canonicalizer extension):

  - Alpha-renaming of function-local variables. A rename today is
    treated as an intent change; the Phase 4-5 canonicalizer will
    alpha-rename to canonical `_L0`, `_L1`, ... tokens.
  - Cross-module semantic equivalence (e.g. equivalent expressions
    written with different operators).

Citations record the canonicalizer version that was active when they
were written. Verification re-runs the same version, so future rule
changes do not retroactively invalidate old citations.
"""
from __future__ import annotations

import ast
import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

CANONICALIZER_VERSION = "v1"

_TRAILING_WS_RE = re.compile(r"[ \t]+$", re.MULTILINE)


def _strip_docstring(node: ast.AST) -> None:
    """Remove the leading string-literal expression from a module,
    function, class, or async-function body if present."""
    body = getattr(node, "body", None)
    if not body:
        return
    first = body[0]
    if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
        body.pop(0)


def _walk_strip_docstrings(tree: ast.AST) -> None:
    _strip_docstring(tree)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            _strip_docstring(node)


def _sort_imports(tree: ast.Module) -> None:
    """Sort contiguous runs of top-level import statements alphabetically.

    We only sort stanzas that are purely imports so we do not reorder
    statements across logical boundaries. `from x import a, b` also gets
    its imported-name list sorted.
    """
    if not isinstance(tree, ast.Module):
        return

    def _key(node: ast.AST) -> tuple:
        if isinstance(node, ast.Import):
            names = tuple(sorted(a.name for a in node.names))
            return (0, names)
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            names = tuple(sorted(a.name for a in node.names))
            return (1, mod, node.level, names)
        return (2,)

    i = 0
    body = tree.body
    while i < len(body):
        j = i
        while j < len(body) and isinstance(body[j], (ast.Import, ast.ImportFrom)):
            j += 1
        if j > i + 1:
            stanza = body[i:j]
            for node in stanza:
                if isinstance(node, ast.ImportFrom):
                    node.names = sorted(node.names, key=lambda a: a.name)
                else:
                    node.names = sorted(node.names, key=lambda a: a.name)
            body[i:j] = sorted(stanza, key=_key)
        i = j + 1


def _normalize_source(source: str) -> str:
    s = source.replace("\r\n", "\n").replace("\r", "\n")
    s = _TRAILING_WS_RE.sub("", s)
    return s


def canonicalize_source(source: str, *, version: str = CANONICALIZER_VERSION) -> str:
    """Return a canonical textual form of `source` under the named
    canonicalizer version. Unknown versions raise ValueError so old
    citations never silently re-canonicalize under a newer rule set.
    """
    if version != CANONICALIZER_VERSION:
        raise ValueError(
            f"canonicalizer version {version!r} is not implemented; "
            f"only {CANONICALIZER_VERSION!r} is available at head",
        )
    normalized = _normalize_source(source)
    tree = ast.parse(normalized)
    _walk_strip_docstrings(tree)
    _sort_imports(tree)
    return ast.dump(tree, annotate_fields=True, include_attributes=False)


def hash_intent(source: str, *, version: str = CANONICALIZER_VERSION) -> str:
    """SHA-256 of the canonicalized form of `source`.

    Equal return values for two sources mean their program intent is
    identical under the v1 canonicalizer. Unequal values mean at least
    one logic-bearing difference exists.
    """
    canon = canonicalize_source(source, version=version)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def hash_bytes(data: bytes) -> str:
    """Plain byte-hash for non-source artifacts (JSON fixtures, raw
    files). Kept separate from `hash_intent` so callers cannot
    accidentally elide semantic diffs in data files."""
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class Citation:
    """One pointer from a decision to its source artifact.

    `source_hash` is either `hash_intent(source)` for Python code or
    `hash_bytes(data)` for data files. The `canonicalizer_version`
    field records which rule set produced the hash; verification must
    re-run the same version.
    """

    source_path: str
    source_hash: str
    canonicalizer_version: str = CANONICALIZER_VERSION
    annotation: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def verify_against(self, current_source: str) -> bool:
        """Re-hash `current_source` under the same canonicalizer and
        compare. Returns True on match. Callers decide what to do on
        mismatch; Week 4-5's evaluator treats it as `state_drift_detected`
        and auto-denies."""
        return hash_intent(current_source, version=self.canonicalizer_version) == self.source_hash
