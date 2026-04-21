"""
core/primitives/settlement_ledger.py — Ticket 9 settlement event ledger
=======================================================================

Append-only JSONL + markdown companion for every lock/release/slash event
emitted by a settlement adapter. Mirrors the shape and persistence idioms
of `core.scenario_ledger` (dual persistence — durable JSONL + per-event
markdown for human review).

Shape:

    SettlementEvent(
        event_id,              # uuid4().hex
        ts,                    # ISO-8601 UTC-Z `YYYY-MM-DDTHH:MM:SSZ`
        kind,                  # "lock" | "release" | "slash"
        handle_id,             # EscrowHandle.handle_id
        asset_id,              # AssetRef.asset_id
        amount_quantity_str,   # Decimal rendered at asset.decimals precision
        principals,            # dict: requester_did / provider_did /
                               #       counterparty_pubkey_hex (may be "")
        sla_id,                # the SLA/ref that caused this event ("" if unknown)
        outcome_receipt,       # None for lock; SettlementReceipt.to_dict()
                               # for release and slash
        metadata,              # freeform extras; default {}
    )

Storage:

    <ledger_dir>/events.jsonl              # append-only durable record
    <ledger_dir>/<event_id>.md             # per-event markdown companion

Atomicity approach:
    The JSONL write uses the **snapshot-then-rename** pattern — we read
    the current contents, append the new line into a temp file, and then
    `tmp.replace(target)` to atomically swap. This matches
    `core.scenario_ledger._write_jsonl_snapshot` and gives us an all-or-
    nothing guarantee at the cost of O(n) re-read per append. That cost
    is acceptable for V0 workloads (hundreds to low-thousands of events).
    If `os.replace` (the rename) raises, the original `events.jsonl`
    stays byte-identical to what it was before the call.

Canonical serialization:
    - `json.dumps(..., sort_keys=True, separators=(",", ":"), ensure_ascii=False)`
    - `Decimal` values rendered via `str(d)`.
    - Datetimes in `YYYY-MM-DDTHH:MM:SSZ`.

Principals handling — why a dict and not kwargs on lock/release/slash:
    The `SettlementAdapter` Protocol (Ticket 0) does not carry principal
    DIDs — it speaks only of locker/recipient/beneficiary strings. Rather
    than widen the Protocol, we accept a `principals` dict on the
    `SettlementEvent` itself; the `MockSettlementAdapter` populates it
    with blanks by default and puts adapter-known strings into
    `metadata`. Ticket 6 (sim refactor) fills real DIDs by passing a
    richer `metadata` when it wires its own recording path.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Literal, Optional

LEDGER_FILENAME = "events.jsonl"

EventKind = Literal[
    "lock",
    "release",
    "slash",
    "verdict_issued",
    "release_from_verdict",
    "slash_from_verdict",
    "refund_from_verdict",
    "founder_override",
]


def _utc_z_now() -> str:
    """Return current time as `YYYY-MM-DDTHH:MM:SSZ`."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _new_event_id() -> str:
    return uuid.uuid4().hex


# ---------------------------------------------------------------------------
# Event dataclass
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SettlementEvent:
    """One immutable settlement event.

    Frozen so callers can safely stash/compare/use as dict keys. Fields
    match the shape documented in Ticket 9's acceptance criteria.
    """

    kind: EventKind
    handle_id: str
    asset_id: str
    amount_quantity_str: str
    sla_id: str = ""
    principals: dict = field(default_factory=dict)
    outcome_receipt: Optional[dict] = None
    metadata: dict = field(default_factory=dict)
    event_id: str = field(default_factory=_new_event_id)
    ts: str = field(default_factory=_utc_z_now)

    def to_dict(self) -> dict[str, Any]:
        """Canonical dict form — sorted-keys JSON over the result yields
        the canonical wire bytes."""
        return {
            "event_id": self.event_id,
            "ts": self.ts,
            "kind": self.kind,
            "handle_id": self.handle_id,
            "asset_id": self.asset_id,
            "amount_quantity_str": self.amount_quantity_str,
            "principals": dict(self.principals),
            "sla_id": self.sla_id,
            "outcome_receipt": (
                dict(self.outcome_receipt)
                if self.outcome_receipt is not None
                else None
            ),
            "metadata": dict(self.metadata),
        }

    def to_canonical_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )


def _event_from_dict(obj: dict) -> SettlementEvent:
    """Rehydrate a `SettlementEvent` from a JSONL line dict."""
    principals = obj.get("principals") or {}
    metadata = obj.get("metadata") or {}
    outcome_receipt = obj.get("outcome_receipt")
    return SettlementEvent(
        event_id=obj.get("event_id", ""),
        ts=obj.get("ts", ""),
        kind=obj.get("kind", "lock"),
        handle_id=obj.get("handle_id", ""),
        asset_id=obj.get("asset_id", ""),
        amount_quantity_str=obj.get("amount_quantity_str", "0"),
        principals=dict(principals),
        sla_id=obj.get("sla_id", ""),
        outcome_receipt=(
            dict(outcome_receipt) if outcome_receipt is not None else None
        ),
        metadata=dict(metadata),
    )


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------
def _render_md(event: SettlementEvent) -> str:
    lines: list[str] = [
        f"# Settlement Event {event.event_id}",
        "",
        f"- **ts**: {event.ts}",
        f"- **kind**: {event.kind}",
        f"- **handle_id**: {event.handle_id}",
        f"- **asset_id**: {event.asset_id}",
        f"- **amount**: {event.amount_quantity_str}",
        f"- **sla_id**: {event.sla_id}",
    ]
    # Principals — emit each known key even when blank so readers can
    # visually confirm the event had no DID info attached.
    principals = event.principals or {}
    for pk in ("requester_did", "provider_did", "counterparty_pubkey_hex"):
        lines.append(f"- **{pk}**: {principals.get(pk, '')}")
    # Any extra principal keys beyond the canonical trio
    extra_principals = {
        k: v for k, v in principals.items()
        if k not in {"requester_did", "provider_did", "counterparty_pubkey_hex"}
    }
    if extra_principals:
        lines.append("")
        lines.append("## Additional Principals")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(extra_principals, sort_keys=True, indent=2))
        lines.append("```")

    lines.append("")
    lines.append("## Outcome Receipt")
    lines.append("")
    if event.outcome_receipt is None:
        lines.append("_omitted (lock event)_")
    else:
        lines.append("```json")
        lines.append(json.dumps(event.outcome_receipt, sort_keys=True, indent=2))
        lines.append("```")

    lines.append("")
    lines.append("## Metadata")
    lines.append("")
    if event.metadata:
        lines.append("```json")
        lines.append(json.dumps(event.metadata, sort_keys=True, indent=2))
        lines.append("```")
    else:
        lines.append("_(empty)_")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------
class SettlementEventLedger:
    """Append-only ledger of `SettlementEvent`s on disk.

    Ensures `ledger_dir` exists at construction. `record` is the only
    mutator; it writes the JSONL line via snapshot-then-rename (so a
    mid-write crash leaves the file either fully prior or fully new,
    never half-written) and then writes a per-event markdown companion.
    """

    def __init__(self, ledger_dir: Path) -> None:
        self.ledger_dir = Path(ledger_dir)
        self.ledger_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Paths
    # ------------------------------------------------------------------
    @property
    def jsonl_path(self) -> Path:
        return self.ledger_dir / LEDGER_FILENAME

    def md_path(self, event_id: str) -> Path:
        return self.ledger_dir / f"{event_id}.md"

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------
    def record(self, event: SettlementEvent) -> None:
        """Append `event` to the ledger. Never overwrites an existing
        JSONL tail — the append is snapshot-then-rename-atomic: if the
        rename fails, `events.jsonl` remains exactly as it was before
        the call. Per-event markdown is written after the JSONL rename
        succeeds."""
        line = event.to_canonical_json() + "\n"
        target = self.jsonl_path
        tmp = target.with_suffix(target.suffix + ".tmp")

        existing = b""
        if target.exists():
            existing = target.read_bytes()

        with tmp.open("wb") as f:
            if existing:
                f.write(existing)
            f.write(line.encode("utf-8"))
            f.flush()
            # fsync best-effort; Windows can surface EINVAL on some FDs
            # depending on the file system, so we swallow it rather than
            # mask the atomic guarantee.
            try:
                import os
                os.fsync(f.fileno())
            except (OSError, AttributeError):
                pass

        # Atomic swap — this is the single point of failure the caller
        # cares about. If it raises, `target` is untouched.
        tmp.replace(target)

        # Markdown companion — separate file, does not affect JSONL
        # atomicity. If this fails, the JSONL is still correct; the
        # exception propagates so tests and callers notice.
        md = self.md_path(event.event_id)
        md.write_text(_render_md(event), encoding="utf-8")

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------
    def iter_events(self) -> Iterator[SettlementEvent]:
        """Stream events from disk in record (file) order. Silently skips
        malformed lines — the ledger is meant to be tolerant to partial
        corruption so reads never turn into an incident."""
        path = self.jsonl_path
        if not path.exists():
            return
        with path.open("r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                yield _event_from_dict(obj)

    def events(self) -> Iterator[SettlementEvent]:
        """Alias for `iter_events`. Exposes the backing event list for
        callers that prefer the shorter name (e.g. the settlement adapter
        scanning for prior verdicts)."""
        return self.iter_events()

    def load_all(self) -> list[SettlementEvent]:
        return list(self.iter_events())


__all__ = [
    "EventKind",
    "LEDGER_FILENAME",
    "SettlementEvent",
    "SettlementEventLedger",
]
