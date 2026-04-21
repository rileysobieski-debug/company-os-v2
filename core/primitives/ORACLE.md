# Company OS Oracle Architecture (v1a)

The v0 settlement layer (see [SETTLEMENT.md](SETTLEMENT.md)) locks,
releases, and slashes escrow against signed `InterOrgSLA`s, but it does
not decide whether a delivered artifact actually satisfies the SLA.
v1a closes that gap with a three-tier verdict model: **Tier 0
deterministic verification** + **Tier 3 founder arbitration**, with
Tier 1 and Tier 2 reserved for v1b/v1c.

Every verdict is a signed `OracleVerdict`. The settlement adapter's
new method `release_pending_verdict(handle, verdict, ...)` refuses to
act unless the verdict verifies cryptographically and binds the right
SLA + artifact hash. That single entry point is how v1a turns
"someone says it is done" into a hash-verifiable state transition.

## Overview: the three-tier model

| Tier | Who issues | Mechanism | Ships in |
|------|-----------|-----------|----------|
| 0 | Any node running the canonical `SchemaVerifier` | JSON Schema 2020-12 validation + artifact-hash binding, zero LLM calls | **v1a** |
| 1 | An SLA-nominated primary evaluator | LLM-backed rubric scoring with a challenge window | v1b |
| 2 | An allowlist judge quorum | Weighted vote over Tier 1 challenges | v1c |
| 3 | A `FOUNDER_PRINCIPALS`-registered identity | Human override, supersedes lower tiers | **v1a** |

v1a ships the two tiers that require no probabilistic reasoning:
Tier 0 is pure schema math, Tier 3 is a human backstop. Tier 1 and
Tier 2 exist structurally in the SLA (three reserved fields, see
below) but have no consumers until v1b. Until that code ships, the
reserved fields are dead weight the canonical bytes carry so the
shape never changes.

## (a) End-to-end: Tier 0 happy path

`tests/test_oracle_docs.py` runs this example verbatim.

```python
import hashlib
import json
import tempfile
from pathlib import Path
from decimal import Decimal
from datetime import datetime, timezone

from core.primitives import (
    AssetRegistry, Money, AdapterRegistry, MockSettlementAdapter,
    InterOrgSLA, Ed25519Keypair, NodeRegistry, SettlementEventLedger,
)
from core.primitives.oracle import Oracle
from core.primitives.schema_verifier import SchemaVerifier

# 1. v0 setup: registries + keypairs + ledger.
asset_reg = AssetRegistry()
asset_reg.load(Path("core/primitives/asset_registry"))
usd = asset_reg.get("mock-usd")

tmp = Path(tempfile.mkdtemp(prefix="oracle-doc-"))
adapters = AdapterRegistry(asset_reg)
mock = MockSettlementAdapter(
    supported_assets=(usd,),
    ledger=SettlementEventLedger(tmp / "events"),
)
adapters.register(mock)

nodes = NodeRegistry()
nodes.load(tmp / "nodes")

req_kp = Ed25519Keypair.generate()
prov_kp = Ed25519Keypair.generate()
nodes.register("did:companyos:req", req_kp.public_key)
nodes.register("did:companyos:prov", prov_kp.public_key)

# 2. Fund the requester and build a schema-typed SLA.
mock.fund("did:companyos:req", Money(Decimal("0.010000"), usd))
deliverable_schema = {
    "kind": "json_schema",
    "spec_version": "2020-12",
    "schema": {
        "type": "object",
        "required": ["result", "quality_score"],
        "properties": {
            "result": {"type": "string"},
            "quality_score": {"type": "number", "minimum": 0, "maximum": 1},
        },
    },
}
sla = InterOrgSLA.create(
    sla_id="sla-oracle-demo",
    requester_node_did="did:companyos:req",
    provider_node_did="did:companyos:prov",
    task_scope="summarize the 10-K",
    deliverable_schema=deliverable_schema,
    accuracy_requirement=0.9,
    latency_ms=120_000,
    payment=Money(Decimal("0.001000"), usd),
    penalty_stake=Money(Decimal("0.000500"), usd),
    nonce=InterOrgSLA.new_nonce(),
    issued_at=datetime.now(timezone.utc),
    expires_at="2099-01-01T00:00:00Z",
)

# 3. Provider produces the artifact, computes its hash, binds it.
artifact_bytes = json.dumps(
    {"result": "A one-page summary of the 10-K.", "quality_score": 0.97}
).encode("utf-8")
artifact_hash = hashlib.sha256(artifact_bytes).hexdigest()
sla = sla.with_delivery_hash(artifact_hash)

# 4. Requester locks escrow and issues a Tier 0 verdict.
handle = adapters.adapter_for(usd).lock(
    sla.payment, ref=sla.sla_id,
    nonce=InterOrgSLA.new_nonce(),
    principal="did:companyos:req",
)
oracle = Oracle(
    node_did="did:companyos:req",
    node_keypair=req_kp,
    schema_verifier=SchemaVerifier(),
)
verdict = oracle.evaluate_tier0(sla, artifact_bytes)
assert verdict.result == "accepted"
assert verdict.tier == 0
verdict.verify_signature()  # does not raise

# 5. Settle via the verdict. Provider ends up funded.
receipt = adapters.adapter_for(usd).release_pending_verdict(
    handle, verdict,
    expected_artifact_hash=sla.artifact_hash_at_delivery,
    requester_did=sla.requester_node_did,
    provider_did=sla.provider_node_did,
)
assert receipt.outcome == "released"
assert receipt.to == "did:companyos:prov"
```

That sequence is the full v1a happy path. The only non-v0 steps are
`with_delivery_hash`, `Oracle.evaluate_tier0`, and
`release_pending_verdict`. Everything else is the v0 shapes.

## (b) SchemaVerifier: the twelve rulings

Every behavior below is binding on the implementation. Any deviation
is a bug. `core/primitives/schema_verifier.py` implements these and
`tests/test_schema_verifier.py` proves them.

1. **`deliverable_schema` shape.** A discriminated union with a `kind`
   field. v1a supports `kind: "json_schema"` only. Unknown kinds
   (`"executable_tests"`, `"composite"`) are reserved for v1b and
   return `refunded` with `evidence.kind = "unsupported_schema_kind"`.

2. **Binary artifacts.** Use `deliverable_schema.artifact_format = "binary"`.
   The verifier then expects the caller to pass an
   `artifact_properties: dict` via kwarg; the schema validates that
   dict, not the raw bytes. Missing properties on a declared-binary
   artifact returns `refunded` with `"sla_missing_schema"`.

3. **Artifact-hash binding is step 1.** Before any schema work, the
   verifier checks `sha256(artifact_bytes) == sla.artifact_hash_at_delivery`.
   A mismatch returns `rejected` with `"hash_mismatch"`. An unpopulated
   `artifact_hash_at_delivery` (empty string) returns `refunded` with
   `"sla_missing_schema"`.

4. **Determinism.** `SchemaVerifier.verify` is a pure function: no
   clock, no network, no randomness, no LLM, no ambient filesystem
   reads. A Hypothesis property test asserts byte-identical output
   across independent invocations.

5. **Outcome table.** Every malformed input has a deterministic result:

   | Input defect | `result` | `evidence.kind` |
   |---|---|---|
   | Malformed JSON Schema in SLA | `refunded` | `sla_schema_malformed` |
   | Artifact fails JSON decode (JSON expected) | `rejected` | `artifact_parse_error` |
   | SLA has no `deliverable_schema` field | `refunded` | `sla_missing_schema` |
   | Schema validation fails | `rejected` | `schema_fail` |
   | Schema validation passes | `accepted` | `schema_pass` |
   | Hash mismatch | `rejected` | `hash_mismatch` |
   | Unknown `kind` | `refunded` | `unsupported_schema_kind` |
   | Unknown `spec_version` | `refunded` | `unsupported_schema_version` |

   `refunded` outcomes always point at an SLA-drafter deficiency;
   the provider is never penalized for a malformed contract.

6. **`accuracy_requirement` is ignored in v1a.** Tier 0 does not
   score. `verdict.score` stays `None`. Tier 1 (v1b) is what will
   read `accuracy_requirement`. SLA drafters should not expect rubric
   scoring from Tier 0.

7. **JSON Schema version.** Only `spec_version: "2020-12"` is
   supported. Other values return `refunded` with
   `"unsupported_schema_version"`.

8. **Append-only ledger invariants.** `release_pending_verdict` records:
   ```
   lock  ->  verdict_issued  ->  release_from_verdict | slash_from_verdict | refund_from_verdict
   ```
   On a Tier 3 founder override, the sequence extends:
   ```
   ... verdict_issued (tier=0)  ->  <slash_or_refund>  ->
       verdict_issued (tier=3)  ->  founder_override  ->  release_from_verdict
   ```
   The prior verdict stays in the ledger verbatim; the override is a
   new event with explicit `supersedes`. The adapter refuses to act
   on a verdict if the sla_id, artifact hash, or signature does not
   match.

9. **Double-verdict prevention.** Only one Tier 0 or Tier 1 verdict
   per SLA. A second machine verdict raises
   `VerdictError("verdict already issued")`. Only a Tier 3 founder
   override can supersede, and its evidence must carry
   `overrides = prior_verdict.verdict_hash`.

10. **Founder override authority.** `Oracle.founder_override` accepts
    a `founder_keypair` and a `founder_identity` string. The identity
    must be in `state.FOUNDER_PRINCIPALS`. The keypair signs the
    override verdict. The original verdict is preserved in the ledger;
    the override is a new signed event.

11. **Testability.** Every row in table 5 has a named test case in
    `tests/test_schema_verifier.py`. The fixture library covers every
    malformed SLA and every malformed artifact; all cases are
    deterministic and required green.

12. **Evidence schema stability.** `evidence.kind` is a Literal
    discriminator. All valid values are enumerated in
    `core.primitives.oracle.EvidenceKind`. Adding a value requires
    a `protocol_version` bump on `OracleVerdict`. Unknown kinds on
    deserialization raise `VerdictError`.

## (c) Founder override (Tier 3)

The founder path exists for every outcome the machine gets wrong.
Use it when Tier 0 correctly applied the letter of the SLA but the
spirit was missed, or when two counterparties want to settle on
terms the canonical verifier cannot express.

```python
# Continuing from the happy path: assume Tier 0 returned rejected.
tier0 = oracle.evaluate_tier0(sla, artifact_bytes)
assert tier0.result == "rejected"  # e.g. schema_fail

founder_kp = Ed25519Keypair.generate()
tier3 = oracle.founder_override(
    prior_verdict=tier0,
    result="accepted",
    reason="edge case: quality_score 0.899 rounds to 0.9 per convention",
    founder_keypair=founder_kp,
    founder_identity="founder",
)
assert tier3.tier == 3
assert tier3.evidence["kind"] == "founder_override"
assert tier3.evidence["overrides"] == tier0.verdict_hash
assert tier3.evidence["reason"].startswith("edge case")
tier3.verify_signature()  # signed by the founder keypair
```

A non-founder identity raises `SignatureError`:

```python
try:
    oracle.founder_override(
        prior_verdict=tier0,
        result="accepted",
        reason="oops",
        founder_keypair=Ed25519Keypair.generate(),
        founder_identity="mallory",
    )
except SignatureError:
    pass  # expected
```

Authorization in v1a is name-based (the identity string is the
claim). Binding a keypair to a founder identity cryptographically is
deferred to v1b, consistent with how `state._has_founder_signature`
currently trusts `updated_by`.

## (d) Third-party replay

Given the settlement ledger + the artifact bytes + the SLA, any
independent node must be able to reconstruct and re-verify every
verdict without the original issuer's private key.

Replay is mechanical:

1. Load `events.jsonl`. Filter for `kind == "verdict_issued"`.
2. For each event, rebuild an `OracleVerdict` from
   `event.metadata["verdict"]` via `OracleVerdict.from_dict(d)`.
3. Call `verdict.verify_signature()`. No raise means the verdict
   is authentic.
4. Optionally: recompute Tier 0 with a fresh `Oracle` instance
   against the same (sla, artifact_bytes) and assert the result
   agrees. Tier 0 is deterministic, so two independently-run
   verifiers produce identical `result` and `evidence` tuples.

`agent-settlement-sim/tests/test_oracle_replay.py` implements this
end-to-end. The guarantee is: if the ledger has an event recording
a verdict, and the founder has not issued a Tier 3 override, the
verdict's signature proves exactly what the issuer committed to at
the time of issuance.

## (e) What v1a does NOT do

Deliberate v1a non-goals, documented so callers do not plan
around them:

- **No LLM calls.** Tier 0 is schema math and hash comparison.
  LLMs are not bit-deterministic even at `temperature=0` (CUDA fp
  reduction ordering, MoE routing, silent model updates). Any
  design assuming "both sides run the same LLM and agree within
  epsilon" breaks in production. v1a therefore writes LLM
  verification out of scope. Tier 1 (v1b) adds a canonical
  evaluator with a mechanical challenge right, not peer agreement.

- **No challenge window mechanism.** The SLA carries a
  `challenge_window_sec` field (default 86400, range 60 to 604800),
  but v1a has no Tier 1 verdict to challenge. Tier 0 is deterministic
  and final; Tier 3 is a human override and also final. The field
  exists so the canonical SLA shape is stable before v1b ships.

- **No judge quorum.** Tier 2 is reserved. `NodeRegistry` has no
  `is_judge` flag, no arbitration bond. Allowlisting a jury pool
  before real dispute volume reveals the gap would be premature.

- **No executable-test schema kind.** `kind: "executable_tests"` is
  reserved. Running arbitrary test code inside a verifier opens a
  sandbox question v1a does not answer.

- **No binary-artifact content validators.** Binary artifacts validate
  against a `artifact_properties` dict the provider populates. The
  verifier does not decode PDFs, images, or other binary formats.

- **No on-chain or real-network I/O.** `MockSettlementAdapter`
  remains the reference implementation. `StablecoinStubAdapter`
  raises `NotImplementedError` on `release_pending_verdict`.

- **No dispute window on release for v1a.** Tier 0 is deterministic,
  so there is nothing to dispute mechanically. The window in the
  SLA elapses trivially in v1a.

- **No NodeRegistry-backed authorization on `evaluator_did`.**
  `OracleVerdict.verify_signature` proves a keypair signed the bytes.
  It does not prove the keypair is the pubkey registered for
  `evaluator_did`. A verdict that claims `evaluator_did = did:X` but
  is signed by `keypair_Y` passes crypto verification today. See
  `tests/test_oracle_adversarial.py::TestKnownV1aGaps` for the pinned
  demonstration. v1b must resolve `evaluator_did` through the
  `NodeRegistry` and reject on pubkey mismatch, mirroring the pattern
  `InterOrgSLA.verify_signatures(registry=...)` already uses.

- **No version-keyed canonicalizer registry.** Canonical-bytes rules
  live on the current `oracle.py` module. If v1b changes those rules
  (e.g., a new serialization for `score` or `evidence`), historical
  v1a verdicts written under `protocol_version ==
  "companyos-verdict/0.1"` will fail verification under the new rules.
  v1b should dispatch byte derivation through a registry keyed on
  `protocol_version` so archived verdicts stay auditable across
  version bumps.

- **No KMS-backed signer abstraction.** `Oracle.founder_override`
  accepts a raw `Ed25519Keypair`, forcing the founder's private key
  into process memory at call time. A production founder workflow
  should wrap the signing surface in a `Signer` protocol so the
  concrete implementation can be a local keypair, an HSM, or a cloud
  KMS. v1b territory.

The architectural principle behind every cut above: do not build a
cryptoeconomic jury pool before there are SLAs to disputatize. v1a
ships exactly what the ~80% of mechanically-decidable B2B agent
SLAs need, with a clean founder escape hatch for the rest.

## (f) Canonical serialization supplement

`OracleVerdict._canonical_bytes` extends the rules in
[SETTLEMENT.md](SETTLEMENT.md) with three verdict-specific additions:

- `signer` serializes as `{"bytes_hex": "..."}`, not a raw string.
- `signature` is excluded from canonical bytes (same pattern as the
  SLA signature fields).
- `evidence` is a dict; nested dicts are recursively key-sorted via
  `json.dumps(sort_keys=True)`.
- `score: None` serializes as `"score": null` (never omitted) so the
  shape is stable across scored and unscored verdicts.
- `verdict_hash` is excluded during its own computation (the
  chicken-and-egg same pattern as `integrity_binding` on the SLA).

`verdict_hash = sha256(canonical_bytes_without_hash).hexdigest()`.
The Ed25519 signature covers the canonical bytes *with* the
verdict hash included and the signature excluded, so the signature
commits to every content field without self-reference.
