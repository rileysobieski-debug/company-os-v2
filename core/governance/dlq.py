"""Dead-letter queue for governance writes.

Every governance write (decision row, trust snapshot, verdict record,
escalation manifest) must be lossless: if the primary DB refuses the
write, the record buffers here and a background worker drains it on
recovery. The app refuses to serve requests until the backlog is zero
so a partial failure cannot silently leave the audit chain
inconsistent.

Storage is an append-only JSON-Lines file at
`<tenant_root>/governance/dlq.log`. JSONL is chosen over SQLite
because the DLQ is the recovery path for SQLite failures themselves;
using the same backend would defeat the purpose. Every write is
flushed + fsync'd before the caller returns so an OS crash mid-write
cannot drop the line.

Entry shape:

    {
      "enqueued_at": "2026-04-24T...",
      "kind": "decision" | "trust_snapshot" | ... ,
      "payload": { ... JSON-serializable ... },
      "last_error": "sqlite3.OperationalError: database is locked",
      "retry_count": 0,
      "drained_at": null
    }

A drained row is rewritten in place with `drained_at` set. Because
JSONL is append-only, "in place" means we write a new compact file
(kept tombstones) during `rotate()`; live operations only append.
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Iterator


class DLQError(RuntimeError):
    """Base class for DLQ failures."""


class DLQBacklogNotEmpty(DLQError):
    """Raised by startup checks when the DLQ has undrained entries.
    Serve-gating logic: refuse to serve until drain completes."""


@dataclass
class DLQEntry:
    enqueued_at: str
    kind: str
    payload: dict
    last_error: str = ""
    retry_count: int = 0
    drained_at: str | None = None
    entry_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def to_json_line(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, default=str) + "\n"

    @classmethod
    def from_json_line(cls, line: str) -> "DLQEntry":
        data = json.loads(line)
        return cls(
            enqueued_at=data["enqueued_at"],
            kind=data["kind"],
            payload=data["payload"],
            last_error=data.get("last_error", ""),
            retry_count=int(data.get("retry_count", 0)),
            drained_at=data.get("drained_at"),
            entry_id=data.get("entry_id", str(uuid.uuid4())),
        )


class DeadLetterQueue:
    """File-backed DLQ. One instance per tenant (or one per process if
    tenant binding lives in the connection adapter instead)."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        # Touch so startup checks can distinguish "no DLQ yet" from
        # "DLQ exists and is empty".
        self._path.touch(exist_ok=True)

    @property
    def path(self) -> Path:
        return self._path

    def enqueue(self, *, kind: str, payload: dict, last_error: str = "") -> DLQEntry:
        """Buffer a failed write. Flush + fsync before returning so a
        crash immediately after cannot drop the line."""
        entry = DLQEntry(
            enqueued_at=datetime.now(timezone.utc).isoformat(),
            kind=kind,
            payload=payload,
            last_error=last_error,
        )
        line = entry.to_json_line()
        with self._lock:
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(line)
                fh.flush()
                try:
                    os.fsync(fh.fileno())
                except (AttributeError, OSError):
                    # fsync can fail on Windows text-mode handles or on
                    # some filesystems; the flush above is already a
                    # best-effort guarantee.
                    pass
        return entry

    def iter_undrained(self) -> Iterator[DLQEntry]:
        """Yield every undrained entry in insertion order. Streaming
        read (no full-file load) so a growing DLQ does not blow up
        memory during a long drain."""
        with self._path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = DLQEntry.from_json_line(line)
                except (json.JSONDecodeError, KeyError):
                    # A malformed tombstone must not abort the drain;
                    # skip and let the caller decide whether to log it.
                    continue
                if entry.drained_at is None:
                    yield entry

    def backlog_size(self) -> int:
        return sum(1 for _ in self.iter_undrained())

    def is_empty(self) -> bool:
        return self.backlog_size() == 0

    def require_empty_on_startup(self) -> None:
        if not self.is_empty():
            raise DLQBacklogNotEmpty(
                f"DLQ at {self._path} has undrained entries; "
                "drain completes before the app may serve.",
            )

    def drain(self, handler: Callable[[DLQEntry], bool]) -> int:
        """Call `handler(entry)` for each undrained entry. If the
        handler returns True, mark the entry drained. Returns the
        number of entries actually drained in this pass.

        Drained entries are recorded by rewriting the file with their
        `drained_at` field set. The rewrite is atomic via tmp + replace.
        """
        drained_count = 0
        live_entries: list[DLQEntry] = []
        with self._lock:
            # Load all entries into memory. For extremely large DLQs,
            # the caller should rotate the file first; in practice the
            # DLQ is a failure-mode buffer, not a bulk pipeline.
            with self._path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = DLQEntry.from_json_line(line)
                    except (json.JSONDecodeError, KeyError):
                        continue
                    live_entries.append(entry)

            now = datetime.now(timezone.utc).isoformat()
            out_entries: list[DLQEntry] = []
            for entry in live_entries:
                if entry.drained_at is not None:
                    out_entries.append(entry)
                    continue
                ok = False
                try:
                    ok = handler(entry)
                except Exception as exc:  # noqa: BLE001
                    entry.retry_count += 1
                    entry.last_error = f"{type(exc).__name__}: {exc}"
                    out_entries.append(entry)
                    continue
                if ok:
                    entry.drained_at = now
                    drained_count += 1
                else:
                    entry.retry_count += 1
                out_entries.append(entry)

            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            with tmp.open("w", encoding="utf-8") as fh:
                for entry in out_entries:
                    fh.write(entry.to_json_line())
                fh.flush()
                try:
                    os.fsync(fh.fileno())
                except (AttributeError, OSError):
                    pass
            tmp.replace(self._path)

        return drained_count

    def rotate(self, keep_drained: bool = False) -> int:
        """Compact the log file. If `keep_drained=False`, drop drained
        tombstones to reclaim space. Returns the number of lines after
        rotation."""
        kept: list[DLQEntry] = []
        with self._lock:
            with self._path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = DLQEntry.from_json_line(line)
                    except (json.JSONDecodeError, KeyError):
                        continue
                    if entry.drained_at is not None and not keep_drained:
                        continue
                    kept.append(entry)
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            with tmp.open("w", encoding="utf-8") as fh:
                for entry in kept:
                    fh.write(entry.to_json_line())
                fh.flush()
                try:
                    os.fsync(fh.fileno())
                except (AttributeError, OSError):
                    pass
            tmp.replace(self._path)
        return len(kept)


@contextmanager
def tenant_dlq(tenant_root: Path) -> Iterator[DeadLetterQueue]:
    """Open the DLQ for a tenant root. Convenience wrapper so callers
    do not hard-code the filename layout."""
    path = tenant_root / "governance" / "dlq.log"
    yield DeadLetterQueue(path)


__all__ = [
    "DLQBacklogNotEmpty",
    "DLQEntry",
    "DLQError",
    "DeadLetterQueue",
    "tenant_dlq",
]
