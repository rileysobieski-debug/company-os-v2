"""Memory layer tests: Hardened / Shadow split + 14-day decay.

Uses an in-memory SQLite connection pre-populated with the
inherited_context schema so the Memory primitives can be exercised
without spinning up a full tenant. The real tenant integration is
exercised by the Walls layer test suite; this file focuses on the
hardening semantics.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from core.governance.memory import (
    ContextStatus,
    DECAY_DAYS,
    HardenRefused,
    InheritedRow,
    get_row,
    harden_by_decay,
    harden_explicit,
    iter_rows,
    sweep_decay,
    write_import,
)


@pytest.fixture
def conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE inherited_context (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            imported_at TEXT NOT NULL,
            source_adapter TEXT NOT NULL,
            source_entity_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            hardened INTEGER NOT NULL DEFAULT 0,
            hardened_at TEXT,
            UNIQUE(source_adapter, source_entity_id)
        );
        """,
    )
    return conn


# ---------------------------------------------------------------------------
# write_import
# ---------------------------------------------------------------------------
def test_write_import_inserts_as_shadow(conn) -> None:
    row_id = write_import(
        conn, source_adapter="notion", source_entity_id="page-1",
        kind="page", payload={"title": "Hello"},
    )
    conn.commit()
    row = get_row(conn, row_id)
    assert row.status == ContextStatus.SHADOW
    assert row.payload == {"title": "Hello"}


def test_write_import_upsert_replaces_payload(conn) -> None:
    first = write_import(
        conn, source_adapter="notion", source_entity_id="p1",
        kind="page", payload={"v": 1},
    )
    conn.commit()
    second = write_import(
        conn, source_adapter="notion", source_entity_id="p1",
        kind="page", payload={"v": 2},
    )
    conn.commit()
    assert first == second
    assert get_row(conn, first).payload == {"v": 2}


# ---------------------------------------------------------------------------
# iter_rows filters
# ---------------------------------------------------------------------------
def test_iter_rows_shadow_only(conn) -> None:
    a = write_import(conn, source_adapter="n", source_entity_id="1", kind="k", payload={})
    b = write_import(conn, source_adapter="n", source_entity_id="2", kind="k", payload={})
    conn.commit()
    harden_explicit(conn, a, founder_signature="sig")
    conn.commit()
    shadow_ids = [r.row_id for r in iter_rows(conn, status=ContextStatus.SHADOW)]
    assert shadow_ids == [b]


def test_iter_rows_hardened_only(conn) -> None:
    a = write_import(conn, source_adapter="n", source_entity_id="1", kind="k", payload={})
    b = write_import(conn, source_adapter="n", source_entity_id="2", kind="k", payload={})
    conn.commit()
    harden_explicit(conn, a, founder_signature="sig")
    conn.commit()
    hardened_ids = [r.row_id for r in iter_rows(conn, status=ContextStatus.HARDENED)]
    assert hardened_ids == [a]


def test_iter_rows_source_adapter_filter(conn) -> None:
    write_import(conn, source_adapter="notion", source_entity_id="1", kind="k", payload={})
    qb = write_import(conn, source_adapter="quickbooks", source_entity_id="2", kind="k", payload={})
    conn.commit()
    ids = [r.row_id for r in iter_rows(conn, source_adapter="quickbooks")]
    assert ids == [qb]


# ---------------------------------------------------------------------------
# harden_explicit
# ---------------------------------------------------------------------------
def test_harden_explicit_sets_hardened_at(conn) -> None:
    row_id = write_import(conn, source_adapter="n", source_entity_id="1", kind="k", payload={})
    conn.commit()
    result = harden_explicit(conn, row_id, founder_signature="founder-sig")
    conn.commit()
    assert result.method == "founder-explicit"
    assert result.hardened_at
    assert get_row(conn, row_id).hardened


def test_harden_explicit_empty_signature_refused(conn) -> None:
    row_id = write_import(conn, source_adapter="n", source_entity_id="1", kind="k", payload={})
    conn.commit()
    with pytest.raises(HardenRefused):
        harden_explicit(conn, row_id, founder_signature="")


def test_harden_explicit_is_idempotent(conn) -> None:
    row_id = write_import(conn, source_adapter="n", source_entity_id="1", kind="k", payload={})
    conn.commit()
    first = harden_explicit(conn, row_id, founder_signature="s")
    conn.commit()
    second = harden_explicit(conn, row_id, founder_signature="s")
    assert first.hardened_at == second.hardened_at


# ---------------------------------------------------------------------------
# harden_by_decay
# ---------------------------------------------------------------------------
def test_harden_by_decay_refused_when_too_young(conn) -> None:
    row_id = write_import(conn, source_adapter="n", source_entity_id="1", kind="k", payload={})
    conn.commit()
    with pytest.raises(HardenRefused):
        harden_by_decay(conn, row_id, citation_count=1)


def test_harden_by_decay_refused_when_no_citations(conn) -> None:
    long_ago = (datetime.now(timezone.utc) - timedelta(days=DECAY_DAYS + 1)).isoformat()
    row_id = write_import(
        conn, source_adapter="n", source_entity_id="1",
        kind="k", payload={}, imported_at=long_ago,
    )
    conn.commit()
    with pytest.raises(HardenRefused):
        harden_by_decay(conn, row_id, citation_count=0)


def test_harden_by_decay_permitted_when_aged_and_cited(conn) -> None:
    long_ago = (datetime.now(timezone.utc) - timedelta(days=DECAY_DAYS + 1)).isoformat()
    row_id = write_import(
        conn, source_adapter="n", source_entity_id="1",
        kind="k", payload={}, imported_at=long_ago,
    )
    conn.commit()
    result = harden_by_decay(conn, row_id, citation_count=1)
    conn.commit()
    assert result.method == "automatic-decay"
    assert get_row(conn, row_id).hardened


def test_harden_by_decay_exactly_at_threshold_refused(conn) -> None:
    """Strict > threshold; exactly DECAY_DAYS is still young enough."""
    exactly = (datetime.now(timezone.utc) - timedelta(days=DECAY_DAYS - 1)).isoformat()
    row_id = write_import(
        conn, source_adapter="n", source_entity_id="1",
        kind="k", payload={}, imported_at=exactly,
    )
    conn.commit()
    with pytest.raises(HardenRefused):
        harden_by_decay(conn, row_id, citation_count=1)


# ---------------------------------------------------------------------------
# sweep_decay
# ---------------------------------------------------------------------------
def test_sweep_decay_hardens_eligible_rows(conn) -> None:
    now = datetime.now(timezone.utc)
    aged = (now - timedelta(days=DECAY_DAYS + 2)).isoformat()
    young = (now - timedelta(days=1)).isoformat()

    aged_cited = write_import(
        conn, source_adapter="n", source_entity_id="aged_cited",
        kind="k", payload={}, imported_at=aged,
    )
    write_import(
        conn, source_adapter="n", source_entity_id="aged_uncited",
        kind="k", payload={}, imported_at=aged,
    )
    write_import(
        conn, source_adapter="n", source_entity_id="young",
        kind="k", payload={}, imported_at=young,
    )
    conn.commit()

    def counter(row: InheritedRow) -> int:
        return 2 if row.source_entity_id == "aged_cited" else 0

    report = sweep_decay(conn, counter, now=now)
    assert report.checked == 3
    assert report.hardened == 1
    assert report.skipped_young == 1
    assert report.skipped_no_citation == 1
    assert get_row(conn, aged_cited).hardened


def test_sweep_decay_empty_db_is_noop(conn) -> None:
    report = sweep_decay(conn, lambda _row: 0)
    assert report.checked == 0
    assert report.hardened == 0


# ---------------------------------------------------------------------------
# Row dataclass
# ---------------------------------------------------------------------------
def test_row_status_derived_from_hardened_flag(conn) -> None:
    row_id = write_import(conn, source_adapter="n", source_entity_id="1", kind="k", payload={})
    conn.commit()
    assert get_row(conn, row_id).status == ContextStatus.SHADOW
    harden_explicit(conn, row_id, founder_signature="s")
    conn.commit()
    assert get_row(conn, row_id).status == ContextStatus.HARDENED


def test_row_is_frozen_dataclass(conn) -> None:
    row_id = write_import(conn, source_adapter="n", source_entity_id="1", kind="k", payload={})
    conn.commit()
    row = get_row(conn, row_id)
    with pytest.raises(Exception):
        row.hardened = True  # type: ignore[misc]
