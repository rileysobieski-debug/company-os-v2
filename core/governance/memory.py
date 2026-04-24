"""Hardened vs Shadow context split with 14-day decay.

Ambient observations (chat logs, scenario notes, import-adapter rows)
start as **shadow** context: `hardened=0`. Shadow context is visible
to agents for cross-pollination but does NOT feed the evaluator's
hard-constraint gate; a shadow row cannot block a request.

A shadow row graduates to **hardened** context when BOTH:

  1. 14 days have elapsed without a contradicting observation.
  2. At least one citation in a successful transaction names it.

Only hardened context is treated as ground truth. This split is the
v6 Gemini-round resolution for the "non-deterministic fuel source"
problem: transition-mode imports bring in fuzzy historical state that
cannot participate in deterministic gating until a founder vouches for
it or the decay invariant confirms it.

Founder can also harden a row explicitly via the `harden_inherited_context`
action type (requires evidence kind `founder_review`). Explicit hardens
bypass the 14-day clock.

Storage is `inherited_context` table in the tenant DB (see migration
001). This module owns the read / write / decay semantics; the DB
schema itself is migration-controlled.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Iterable


class ContextNotFound(LookupError):
    """Raised when a referenced shadow / hardened row does not exist."""


class HardenRefused(RuntimeError):
    """Raised when an automatic harden request does not meet the
    14-day + citation invariant. Callers catch and either wait or
    escalate via the `harden_inherited_context` action type for a
    founder-signed override."""


DECAY_DAYS: int = 14


class ContextStatus(Enum):
    SHADOW = "shadow"
    HARDENED = "hardened"


@dataclass(frozen=True)
class InheritedRow:
    row_id: int
    imported_at: str
    source_adapter: str
    source_entity_id: str
    kind: str
    payload: dict
    hardened: bool
    hardened_at: str | None

    @property
    def status(self) -> ContextStatus:
        return ContextStatus.HARDENED if self.hardened else ContextStatus.SHADOW


@dataclass(frozen=True)
class HardenResult:
    row_id: int
    hardened_at: str
    method: str  # "automatic-decay" | "founder-explicit"


def write_import(
    conn: sqlite3.Connection,
    *,
    source_adapter: str,
    source_entity_id: str,
    kind: str,
    payload: dict,
    imported_at: str | None = None,
) -> int:
    """UPSERT an imported entity as shadow context. Returns the row id.
    Caller is responsible for committing."""
    imported_at = imported_at or datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        """
        INSERT INTO inherited_context (
            imported_at, source_adapter, source_entity_id,
            kind, payload_json, hardened
        )
        VALUES (?, ?, ?, ?, ?, 0)
        ON CONFLICT(source_adapter, source_entity_id) DO UPDATE SET
            imported_at = excluded.imported_at,
            kind = excluded.kind,
            payload_json = excluded.payload_json
        """,
        (imported_at, source_adapter, source_entity_id, kind, json.dumps(payload, sort_keys=True)),
    )
    if cur.lastrowid:
        return cur.lastrowid
    row = conn.execute(
        "SELECT id FROM inherited_context WHERE source_adapter = ? AND source_entity_id = ?",
        (source_adapter, source_entity_id),
    ).fetchone()
    if row is None:
        raise ContextNotFound(
            f"write_import upsert failed to locate row for "
            f"{source_adapter}:{source_entity_id}",
        )
    return int(row[0])


def get_row(conn: sqlite3.Connection, row_id: int) -> InheritedRow:
    row = conn.execute(
        """
        SELECT id, imported_at, source_adapter, source_entity_id,
               kind, payload_json, hardened, hardened_at
          FROM inherited_context
         WHERE id = ?
        """,
        (row_id,),
    ).fetchone()
    if row is None:
        raise ContextNotFound(f"no inherited_context row with id={row_id}")
    return InheritedRow(
        row_id=int(row[0]),
        imported_at=row[1],
        source_adapter=row[2],
        source_entity_id=row[3],
        kind=row[4],
        payload=json.loads(row[5]),
        hardened=bool(row[6]),
        hardened_at=row[7],
    )


def iter_rows(
    conn: sqlite3.Connection,
    *,
    status: ContextStatus | None = None,
    source_adapter: str | None = None,
) -> Iterable[InheritedRow]:
    clauses = []
    params: list[object] = []
    if status is ContextStatus.HARDENED:
        clauses.append("hardened = 1")
    elif status is ContextStatus.SHADOW:
        clauses.append("hardened = 0")
    if source_adapter is not None:
        clauses.append("source_adapter = ?")
        params.append(source_adapter)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    for row in conn.execute(
        f"""
        SELECT id, imported_at, source_adapter, source_entity_id,
               kind, payload_json, hardened, hardened_at
          FROM inherited_context {where}
         ORDER BY id ASC
        """,
        params,
    ):
        yield InheritedRow(
            row_id=int(row[0]),
            imported_at=row[1],
            source_adapter=row[2],
            source_entity_id=row[3],
            kind=row[4],
            payload=json.loads(row[5]),
            hardened=bool(row[6]),
            hardened_at=row[7],
        )


def harden_explicit(
    conn: sqlite3.Connection,
    row_id: int,
    *,
    founder_signature: str,
    hardened_at: str | None = None,
) -> HardenResult:
    """Founder-signed hardening. Bypasses the 14-day clock because a
    human signature is sufficient evidence on its own. The `founder_signature`
    is expected to be a KMS-signed string; the evaluator's `founder_override`
    action type produces this signature."""
    if not founder_signature:
        raise HardenRefused("founder_signature is required for explicit hardening")
    row = get_row(conn, row_id)
    if row.hardened:
        return HardenResult(
            row_id=row_id,
            hardened_at=row.hardened_at or "",
            method="founder-explicit",
        )
    now = hardened_at or datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE inherited_context SET hardened = 1, hardened_at = ? WHERE id = ?",
        (now, row_id),
    )
    return HardenResult(row_id=row_id, hardened_at=now, method="founder-explicit")


def harden_by_decay(
    conn: sqlite3.Connection,
    row_id: int,
    *,
    citation_count: int,
    now: datetime | None = None,
) -> HardenResult:
    """Automatic hardening via the 14-day-no-contradiction invariant.
    Requires `citation_count >= 1` (the row has been referenced in at
    least one successful transaction) AND the row is older than
    `DECAY_DAYS`. Callers supply `citation_count` via the decisions
    table lookup."""
    now = now or datetime.now(timezone.utc)
    row = get_row(conn, row_id)
    if row.hardened:
        return HardenResult(
            row_id=row_id,
            hardened_at=row.hardened_at or "",
            method="automatic-decay",
        )
    if citation_count < 1:
        raise HardenRefused(
            f"row {row_id} has zero citations; automatic hardening "
            "requires at least one successful transaction to reference it",
        )
    imported = _parse_iso(row.imported_at)
    if now - imported < timedelta(days=DECAY_DAYS):
        raise HardenRefused(
            f"row {row_id} is {(now - imported).days} days old; "
            f"decay invariant requires >= {DECAY_DAYS} days",
        )
    now_iso = now.isoformat()
    conn.execute(
        "UPDATE inherited_context SET hardened = 1, hardened_at = ? WHERE id = ?",
        (now_iso, row_id),
    )
    return HardenResult(row_id=row_id, hardened_at=now_iso, method="automatic-decay")


@dataclass
class DecaySweepReport:
    checked: int = 0
    hardened: int = 0
    skipped_young: int = 0
    skipped_no_citation: int = 0
    errors: list[str] = field(default_factory=list)


def sweep_decay(
    conn: sqlite3.Connection,
    citation_counter: "callable[[InheritedRow], int]",
    *,
    now: datetime | None = None,
) -> DecaySweepReport:
    """Walk every shadow row. For each, check the 14-day + citation
    invariant and harden when both are met. Returns a structured
    report so the caller can surface counts to the Tension HUD."""
    now = now or datetime.now(timezone.utc)
    report = DecaySweepReport()
    for row in iter_rows(conn, status=ContextStatus.SHADOW):
        report.checked += 1
        imported = _parse_iso(row.imported_at)
        if now - imported < timedelta(days=DECAY_DAYS):
            report.skipped_young += 1
            continue
        count = citation_counter(row)
        if count < 1:
            report.skipped_no_citation += 1
            continue
        try:
            harden_by_decay(conn, row.row_id, citation_count=count, now=now)
            report.hardened += 1
        except HardenRefused as exc:
            report.errors.append(f"{row.row_id}: {exc}")
    return report


def _parse_iso(timestamp: str) -> datetime:
    # Handle trailing Z by swapping to +00:00 so datetime.fromisoformat
    # accepts it on all Python versions we support.
    ts = timestamp.strip()
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


__all__ = [
    "ContextNotFound",
    "ContextStatus",
    "DECAY_DAYS",
    "DecaySweepReport",
    "HardenRefused",
    "HardenResult",
    "InheritedRow",
    "get_row",
    "harden_by_decay",
    "harden_explicit",
    "iter_rows",
    "sweep_decay",
    "write_import",
]
