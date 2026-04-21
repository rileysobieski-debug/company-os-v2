# Company OS Settlement Architecture (v0)

The Company OS v0 settlement stack is currency-agnostic by
construction: every amount is a `Money(Decimal, AssetRef)`, every
contract is a signed `InterOrgSLA`, every escrow runs through a
`SettlementAdapter` the registry picks by asset. This document shows
the end-to-end happy path, the threat model, the keypair lifecycle,
the audit-ledger format, and where v0 stops.

## (a) End-to-end: sign → register → negotiate → lock → settle

The following is a runnable, ~35-line happy path using only
`core.primitives`. `tests/test_settlement_docs.py` executes this
verbatim.

```python
import tempfile
from pathlib import Path
from decimal import Decimal

from core.primitives import (
    AssetRegistry, Money, AdapterRegistry, MockSettlementAdapter,
    InterOrgSLA, Ed25519Keypair, NodeRegistry, SettlementEventLedger,
)

# 1. Bring up per-node registries (no module-level singletons).
asset_reg = AssetRegistry()
asset_reg.load(Path("core/primitives/asset_registry"))
usd = asset_reg.get("mock-usd")

adapters = AdapterRegistry(asset_reg)
mock = MockSettlementAdapter(supported_assets=(usd,))
adapters.register(mock)

tmp = Path(tempfile.mkdtemp(prefix="settlement-doc-"))
nodes = NodeRegistry()
nodes.load(tmp / "nodes")
ledger = SettlementEventLedger(tmp / "events")

# 2. Generate keypairs, bind DIDs to pubkeys in the NodeRegistry.
req_kp = Ed25519Keypair.generate()
prov_kp = Ed25519Keypair.generate()
nodes.register("did:companyos:req", req_kp.public_key)
nodes.register("did:companyos:prov", prov_kp.public_key)

# 3. Build the unsigned SLA, co-sign, verify against NodeRegistry.
sla = InterOrgSLA.create(
    sla_id="sla-demo-0001",
    requester_node_did="did:companyos:req",
    provider_node_did="did:companyos:prov",
    task_scope="Summarize Q1 wine distribution report",
    deliverable_schema={"format": "markdown"},
    accuracy_requirement=0.95, latency_ms=5000,
    payment=Money(Decimal("0.001000"), usd),
    penalty_stake=Money(Decimal("0.000500"), usd),
    nonce=InterOrgSLA.new_nonce(),
    issued_at="2026-04-19T12:00:00Z",
    expires_at="2026-04-19T13:00:00Z",
)
sla = sla.sign_as_requester(req_kp).sign_as_provider(prov_kp)
sla.verify_signatures(registry=nodes)   # Sybil-resistant check

# 4. Fund provider, lock stake, release on success.
mock.fund("did:companyos:prov", Money(Decimal("0.010000"), usd))
handle = mock.lock(
    sla.penalty_stake, ref=sla.sla_id,
    nonce=sla.nonce, principal="did:companyos:prov",
)
receipt = mock.release(handle, to="did:companyos:prov")
assert receipt.outcome == "released"
```

This is the happy path. The scenario sim in `agent-settlement-sim/`
wraps the same calls inside a state machine that handles failure
(`slash`), mid-run ZKP verification, and audit-ledger emission.

## (b) Threat model

What the v0 stack defends against:

- **Sybil resistance — NodeRegistry.**
  An Ed25519 signature proves *some* keypair signed the bytes. It does
  not prove the signer is the party named in
  `requester_node_did` / `provider_node_did`. `NodeRegistry` closes
  that gap by binding each DID to its canonical pubkey on disk.
  `InterOrgSLA.verify_signatures(registry=nodes)` rejects a signature
  whose embedded `signer` pubkey doesn't match the registered pubkey
  for the claimed DID — so a forged `keypair_B` signing as
  `did:companyos:acme` fails verification.

- **Replay resistance — nonce consumption.**
  `MockSettlementAdapter.lock(..., nonce=...)` tracks consumed nonces
  in `_consumed_nonces`. A replayed SLA (same nonce) raises
  `EscrowStateError("nonce replay detected")`. Nonces are
  append-only — never removed.

- **Tamper detection — signatures + integrity_binding.**
  `integrity_binding` is a SHA-256 over the canonical bytes of the
  SLA (signatures and binding-field excluded). Any tamper — changed
  task_scope, swapped Money, edited nonce — perturbs the hash and
  fails `verify_binding()`. Ed25519 signatures over the canonical
  bytes give the same property cryptographically.

- **Audit — SettlementEventLedger.**
  Every lock / release / slash goes through the adapter which emits
  a `SettlementEvent` to the attached `SettlementEventLedger`. Events
  are append-only JSONL with a markdown companion per event.
  `SettlementEventLedger.load_all()` replays the full sequence.

### What v0 does NOT defend against

- **In-transit wiretap.** v0 ships canonical bytes and signatures in
  the clear. Use TLS externally if the transport isn't already
  encrypted — signatures prove integrity, not confidentiality.
- **Counterparty goes offline mid-SLA.** There is no liveness layer
  that auto-slashes a provider who vanishes between `lock` and
  delivery. Use `expires_at` + lazy evaluation — the caller must
  check expiry before settling.
- **Deliverable-quality dispute (v0 only).** v0 had no inter-node
  oracle for whether the delivered work actually meets its schema.
  The scenario sim cheated with a ZKP stub. **v1a closes this gap**:
  see [ORACLE.md](ORACLE.md) for the Tier 0 schema verifier and the
  Tier 3 founder override. Tier 1 (LLM evaluator with challenge
  window) and Tier 2 (judge quorum) remain out of scope until v1b/v1c.

## (c) Keypair lifecycle

- v0: **generated per sim run** via `Ed25519Keypair.generate()`.
  Private keys live in-memory for the duration of the run and are
  discarded at exit. This is deliberately lightweight — it lets tests
  spin up isolated keypairs without touching a keystore.
- v1: **persisted + published.** Each node keeps its private key in
  local secure storage (OS keychain, HSM, cloud KMS) and publishes
  its pubkey through a gossip layer or a shared `NodeRegistry` root.
  DIDs become routable identities, not just local bindings.

The `NodeRegistry` YAML format is already forward-compat — the
`first_seen` and `notes` fields exist precisely so v1 can record
key-rotation history.

## (c.1) Verdict lifecycle (v1a)

v1a adds `release_pending_verdict(handle, verdict, ...)` to every
`SettlementAdapter`. It refuses to act unless the verdict's signature
verifies, the verdict's `sla_id` matches the escrow handle's ref, and
the verdict's `artifact_hash` matches the hash the caller asserts was
delivered. Event sequences:

- **Tier 0 accepted:** `lock -> verdict_issued -> release_from_verdict`
- **Tier 0 rejected:** `lock -> verdict_issued -> slash_from_verdict`
- **Tier 0 refunded:** `lock -> verdict_issued -> refund_from_verdict`
- **Tier 3 founder override:** prior Tier 0 sequence, then
  `verdict_issued (tier=3) -> founder_override -> release_from_verdict`

See [ORACLE.md](ORACLE.md) for the end-to-end walkthrough, the twelve
binding `SchemaVerifier` rulings, the founder-override flow, and
the third-party replay guarantee.

## (d) Audit-ledger format and replay semantics

The ledger writes two files per event:

```
<ledger_dir>/events.jsonl            # append-only durable record
<ledger_dir>/<event_id>.md           # per-event markdown companion
```

Each JSONL line is canonical:

```
json.dumps(event.to_dict(), sort_keys=True,
           separators=(",", ":"), ensure_ascii=False)
```

Replay is just `SettlementEventLedger.load_all()` — a list of
`SettlementEvent`s in file order. The ledger is tolerant to partial
corruption: malformed lines are silently skipped so a dirty tail never
turns a read into an incident. The write path is snapshot-then-rename
atomic (`tmp.replace(target)`), so a mid-write crash leaves the file
either fully prior or fully new, never half-written.

## (e) Strategic positioning

v0 adapters (`MockSettlementAdapter`, `StablecoinStubAdapter`) are
**internal references**, not production backends. `MockSettlementAdapter`
is pure-Python in-memory state for tests and the sim.
`StablecoinStubAdapter` is a chain-shaped stub whose only functional
method is `supports()` — it proves the Protocol survives a realistic
real-chain shape without schema change.

**v1's real-chain adapter is intended to *wrap* existing agent-wallet
infrastructure — Coinbase AgentKit / CDP, Stripe agent treasury,
Bridge — not reinvent it.** Company OS's contribution is the SLA,
escrow, and governance layer that sits above the raw wallet: the
*B2B contracting and escrow protocol for agents*, not the wallet
itself. A production EVM adapter is ~200 LoC of "translate our
`lock`/`release`/`slash` into their SDK calls"; everything above that
line (InterOrgSLA, NodeRegistry, SettlementEventLedger, the canonical
serialization rules) is the durable contribution.

## (f) Datetime canonicalization note

Canonical bytes drop tz offset and sub-second precision **by design**
— see `core/primitives/sla.py:_canonicalize_datetime`. Every datetime
is normalized to `YYYY-MM-DDTHH:MM:SSZ` before hashing, so two nodes
in different timezones signing the same SLA hash to the same bytes.

This means `integrity_binding` is stable across timezone and
precision variations of the "same" moment, but it also means the
canonical form loses the original offset (+05:30 becomes UTC) and any
microseconds.

If an application needs high-precision or original-offset display
values, add an `original_ts` field at the application layer. Do not
push it through `_canonical_bytes` — `integrity_binding` is
unaffected, and the display layer gets the full fidelity it wants.
