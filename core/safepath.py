"""SafePath: chroot-style filesystem containment for per-tenant reads.

Week 2-3 Walls-layer primitive. Every filesystem path supplied by a
tenant agent is validated through `SafePath(tenant_root).resolve(raw)`
before any read. If the resolved path escapes the tenant root, or the
raw input carries a known escape vector, `SovereignBreach` is raised.
The caller is expected to treat `SovereignBreach` as a terminal event:
log to retrolog, drop the request, surface in the Tension HUD.

Contract:

    SafePath(tenant_root).resolve(relative_path) -> Path

    Accepts a relative path (str or Path). Returns an absolute Path
    guaranteed to live inside `tenant_root.resolve()`. Raises
    `SovereignBreach` when any containment invariant is violated.

Rejection rules (defense in depth, every layer independent):

    1. Null bytes anywhere in the raw string (NUL-splitting attacks).
    2. Percent-encoded traversal sequences (%2e, %2f, %5c). These
       survive at the filesystem layer as literals, but if any
       downstream decoder re-interprets them they become traversal.
       Rejecting pre-emptively closes that vector.
    3. Absolute paths (POSIX or Windows, including drive-anchored and
       UNC `\\\\?\\` prefixes).
    4. Paths whose resolved form is not relative to the tenant root.
       Covers `..`, symlink-to-outside, and every combination of
       dots/slashes that resolves upward.

Symlinks are followed (via `Path.resolve`); the resolved target must
still land inside the tenant root. A symlink pointing at a sibling
tenant's data raises `SovereignBreach` naturally via the final
`relative_to` check.

Windows notes:

    - Drive letters like `C:\\Windows` are rejected by the drive-anchor
      check (second char is `:`), regardless of `PurePath.is_absolute()`
      behavior on the current platform.
    - UNC paths (`\\\\server\\share`, `\\\\?\\C:\\...`) are rejected by
      the leading-double-separator check.
    - Reserved device names (CON, PRN, AUX) raise OSError during resolve
      on some Windows versions; we catch and re-raise as SovereignBreach
      so callers get a uniform signal.
"""
from __future__ import annotations

import re
from pathlib import Path, PurePath


class SovereignBreach(Exception):
    """Raised when a resolved path escapes its tenant root. A breach
    kills the request and triggers an alert in the retrolog layer."""


_SUSPICIOUS_ENCODED = re.compile(r"%(?:2[eEfF]|5[cC])")


class SafePath:
    """Chroot-style containment for tenant filesystem access."""

    def __init__(self, tenant_root: str | Path) -> None:
        root = Path(tenant_root)
        try:
            self._root = root.resolve(strict=True)
        except (OSError, FileNotFoundError) as exc:
            raise SovereignBreach(
                f"tenant root does not resolve: {root!r} ({exc})",
            ) from exc

    @property
    def root(self) -> Path:
        return self._root

    def resolve(self, relative: str | Path) -> Path:
        raw = str(relative)

        if "\x00" in raw:
            raise SovereignBreach(f"null byte in path: {raw!r}")

        if _SUSPICIOUS_ENCODED.search(raw):
            raise SovereignBreach(
                f"suspicious percent-encoded traversal sequence: {raw!r}",
            )

        if raw.startswith(("\\\\", "//")):
            raise SovereignBreach(f"UNC or double-separator path rejected: {raw!r}")

        if len(raw) >= 2 and raw[1] == ":":
            raise SovereignBreach(f"drive-anchored path rejected: {raw!r}")

        try:
            candidate = PurePath(raw)
        except (ValueError, TypeError) as exc:
            raise SovereignBreach(f"path could not be parsed: {raw!r} ({exc})") from exc

        if candidate.is_absolute():
            raise SovereignBreach(f"absolute path rejected: {raw!r}")

        joined = self._root / raw
        try:
            resolved = joined.resolve(strict=False)
        except (OSError, ValueError, RuntimeError) as exc:
            raise SovereignBreach(
                f"path could not be resolved: {raw!r} ({exc})",
            ) from exc

        try:
            resolved.relative_to(self._root)
        except ValueError as exc:
            raise SovereignBreach(
                f"path escapes tenant root: {raw!r} -> {resolved!r}",
            ) from exc

        return resolved
