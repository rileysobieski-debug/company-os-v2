"""DeadLetterQueue tests."""
from __future__ import annotations

from pathlib import Path

import pytest

from core.governance.dlq import (
    DLQBacklogNotEmpty,
    DLQEntry,
    DeadLetterQueue,
    tenant_dlq,
)


@pytest.fixture
def dlq(tmp_path: Path) -> DeadLetterQueue:
    return DeadLetterQueue(tmp_path / "governance" / "dlq.log")


def test_empty_dlq_starts_empty(dlq: DeadLetterQueue) -> None:
    assert dlq.is_empty()
    assert dlq.backlog_size() == 0


def test_enqueue_returns_entry_with_timestamp(dlq: DeadLetterQueue) -> None:
    entry = dlq.enqueue(kind="decision", payload={"id": 1})
    assert isinstance(entry, DLQEntry)
    assert entry.enqueued_at
    assert entry.kind == "decision"
    assert entry.payload == {"id": 1}
    assert entry.drained_at is None


def test_enqueue_persists_across_instances(tmp_path: Path) -> None:
    path = tmp_path / "dlq.log"
    first = DeadLetterQueue(path)
    first.enqueue(kind="decision", payload={"id": 1})
    second = DeadLetterQueue(path)
    assert second.backlog_size() == 1


def test_require_empty_on_startup_passes_when_empty(dlq: DeadLetterQueue) -> None:
    dlq.require_empty_on_startup()


def test_require_empty_on_startup_raises_when_backlog(dlq: DeadLetterQueue) -> None:
    dlq.enqueue(kind="decision", payload={"id": 1})
    with pytest.raises(DLQBacklogNotEmpty):
        dlq.require_empty_on_startup()


def test_drain_marks_successful_entries(dlq: DeadLetterQueue) -> None:
    dlq.enqueue(kind="a", payload={"i": 1})
    dlq.enqueue(kind="b", payload={"i": 2})

    calls: list[str] = []

    def handler(entry: DLQEntry) -> bool:
        calls.append(entry.kind)
        return True

    drained = dlq.drain(handler)
    assert drained == 2
    assert dlq.is_empty()
    assert calls == ["a", "b"]


def test_drain_respects_failed_handler(dlq: DeadLetterQueue) -> None:
    dlq.enqueue(kind="a", payload={"i": 1})
    dlq.enqueue(kind="b", payload={"i": 2})

    def handler(entry: DLQEntry) -> bool:
        return entry.kind == "a"

    drained = dlq.drain(handler)
    assert drained == 1
    assert dlq.backlog_size() == 1


def test_drain_increments_retry_on_handler_exception(dlq: DeadLetterQueue) -> None:
    dlq.enqueue(kind="a", payload={"i": 1})

    def handler(entry: DLQEntry) -> bool:
        raise RuntimeError("transient")

    drained = dlq.drain(handler)
    assert drained == 0
    remaining = list(dlq.iter_undrained())
    assert len(remaining) == 1
    assert remaining[0].retry_count == 1
    assert "transient" in remaining[0].last_error


def test_drain_is_idempotent_for_drained_entries(dlq: DeadLetterQueue) -> None:
    dlq.enqueue(kind="a", payload={"i": 1})
    dlq.drain(lambda e: True)
    # A second drain pass does nothing: the row has drained_at set.
    calls: list[str] = []
    result = dlq.drain(lambda e: (calls.append(e.kind), True)[1])
    assert result == 0
    assert calls == []


def test_iter_undrained_skips_malformed_lines(dlq: DeadLetterQueue) -> None:
    dlq.enqueue(kind="a", payload={"i": 1})
    # Append a malformed line directly. The DLQ must ignore it rather
    # than aborting the drain; callers decide whether to alert.
    with dlq.path.open("a", encoding="utf-8") as fh:
        fh.write("{not json\n")
    dlq.enqueue(kind="b", payload={"i": 2})
    kinds = [e.kind for e in dlq.iter_undrained()]
    assert kinds == ["a", "b"]


def test_rotate_drops_drained_tombstones_by_default(dlq: DeadLetterQueue) -> None:
    dlq.enqueue(kind="a", payload={"i": 1})
    dlq.enqueue(kind="b", payload={"i": 2})
    dlq.drain(lambda e: e.kind == "a")
    kept = dlq.rotate()
    assert kept == 1
    assert dlq.backlog_size() == 1


def test_rotate_can_preserve_drained_tombstones(dlq: DeadLetterQueue) -> None:
    dlq.enqueue(kind="a", payload={"i": 1})
    dlq.drain(lambda e: True)
    kept = dlq.rotate(keep_drained=True)
    assert kept == 1


def test_tenant_dlq_context_manager_locates_file(tmp_path: Path) -> None:
    with tenant_dlq(tmp_path) as dlq:
        assert dlq.path == tmp_path / "governance" / "dlq.log"
        dlq.enqueue(kind="probe", payload={})
    with tenant_dlq(tmp_path) as dlq:
        assert dlq.backlog_size() == 1


def test_entry_json_round_trip() -> None:
    entry = DLQEntry(
        enqueued_at="2026-04-24T00:00:00+00:00",
        kind="decision",
        payload={"x": 1},
    )
    line = entry.to_json_line()
    rebuilt = DLQEntry.from_json_line(line)
    assert rebuilt.kind == entry.kind
    assert rebuilt.payload == entry.payload
    assert rebuilt.enqueued_at == entry.enqueued_at
