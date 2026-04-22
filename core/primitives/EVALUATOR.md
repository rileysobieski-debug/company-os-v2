# Company OS Evaluator Architecture (v1b)

The v1b Oracle build adds Tier 1 probabilistic evaluation via a named
primary evaluator. This document describes the `PrimaryEvaluator` protocol,
the `EvaluationOutput` type, the `EvaluatorRegistry`, and the reference
`LLMRubricEvaluator` implementation.

For the full Oracle pipeline (mechanical gate, challenge window, Tier 3
override), see [ORACLE.md](ORACLE.md).

## (a) PrimaryEvaluator protocol

```python
from core.primitives.evaluator import PrimaryEvaluator, EvaluationOutput
from core.primitives.sla import InterOrgSLA

class PrimaryEvaluator(Protocol):
    @property
    def evaluator_did(self) -> str: ...

    @property
    def canonical_hash(self) -> str: ...

    def evaluate(
        self,
        sla: InterOrgSLA,
        artifact_bytes: bytes,
        *,
        artifact_properties: dict | None = None,
    ) -> EvaluationOutput: ...
```

The protocol is `runtime_checkable`, so `isinstance(x, PrimaryEvaluator)`
works without subclassing. Any object with these two properties and one method
is automatically conformant.

`evaluator_did` is the DID of the evaluator node. It must not equal the
requester or provider DID in the SLA (no self-evaluation). The Oracle's
`evaluate_tier1` checks this before calling `evaluate`.

`canonical_hash` is a content-addressed fingerprint of the evaluator's
algorithm version (see section (d) below). The SLA commits to this hash;
the adapter rejects verdicts where the hash differs from the SLA commitment.

## (b) EvaluationOutput fields

```python
from decimal import Decimal
from core.primitives.evaluator import EvaluationOutput

output = EvaluationOutput(
    result="accepted",       # "accepted" | "rejected" | "refunded"
    score=Decimal("0.92"),   # numeric score in [0, 1]
    evidence={"kind": "schema_pass_with_score"},  # must be a valid EvidenceKind
    evaluator_canonical_hash="abc123...",  # from evaluator.canonical_hash
)
```

`result` uses the same values as `OracleResult`. The evaluator decides the
outcome; the Oracle does not re-evaluate or override the score.

`evidence["kind"]` must be a valid `EvidenceKind` (see `oracle.py`). The
typical value for a successful Tier 1 evaluation is `"schema_pass_with_score"`.
For internal errors, use `"evaluator_error"` with a `"detail"` key. Do not
use `"evaluator_timeout"` -- the Oracle sets that itself when the evaluator
exceeds `evaluator_timeout_sec`.

`EvaluationOutput` validates `evidence["kind"]` at construction time via
`__post_init__`. Unknown kinds raise `ValueError` immediately.

## (c) Implementing a custom evaluator

```python
from decimal import Decimal
from core.primitives.evaluator import EvaluationOutput, PrimaryEvaluator
from core.primitives.sla import InterOrgSLA

class MyEvaluator:
    @property
    def evaluator_did(self) -> str:
        return "did:companyos:my-evaluator"

    @property
    def canonical_hash(self) -> str:
        # Return a stable hash of this evaluator's algorithm version.
        # Must match what the SLA's canonical_evaluator_hash was set to.
        return "a" * 64  # 64 lowercase hex chars

    def evaluate(
        self,
        sla: InterOrgSLA,
        artifact_bytes: bytes,
        *,
        artifact_properties: dict | None = None,
    ) -> EvaluationOutput:
        # Run your scoring logic here.
        score = Decimal("0.95")
        result = "accepted" if score >= Decimal(str(sla.accuracy_requirement)) else "rejected"
        return EvaluationOutput(
            result=result,
            score=score,
            evidence={"kind": "schema_pass_with_score"},
            evaluator_canonical_hash=self.canonical_hash,
        )

assert isinstance(MyEvaluator(), PrimaryEvaluator)  # True
```

Error handling: if your evaluator encounters a recoverable error, return a
`refunded` result with `evidence={"kind": "evaluator_error", "detail": "..."}`.
Do not raise exceptions -- the Oracle catches uncaught exceptions and may
leave the escrow in an indeterminate state. Return a clean `EvaluationOutput`
always.

## (d) canonical_hash contract

The `canonical_hash` is a content-addressed identifier for a specific version
of the evaluator's algorithm. The rules:

- It must be exactly 64 lowercase hex characters (SHA-256 output).
- It must be STABLE for a given algorithm version: the same code, model, and
  configuration always produce the same hash.
- It must CHANGE when any of the following change: the scoring logic, the
  model identifier, the rubric text, the score floor, or any other factor that
  would produce different scores on the same artifact.
- The SLA's `canonical_evaluator_hash` field commits to a specific hash.
  Counterparties sign the SLA knowing which evaluator version they are
  accepting. A hash drift is treated as an unauthorized evaluator substitution.

The adapter enforces: `verdict.evidence["evaluator_canonical_hash"]` must equal
`sla.canonical_evaluator_hash` when `expected_evaluator_canonical_hash` is
passed to `release_pending_verdict`.

## (e) LLMRubricEvaluator (reference implementation)

`core/primitives/evaluators/llm_rubric.py` is the reference implementation.
It uses an LLM (Anthropic Claude) to score an artifact against a rubric.

Constructor:

```python
from core.primitives.evaluators.llm_rubric import LLMRubricEvaluator

evaluator = LLMRubricEvaluator(
    evaluator_did="did:companyos:my-llm-evaluator",
    model="claude-sonnet-4-6",
    rubric="The summary must cover all main financial highlights ...",
    floor=0.9,            # reject if score < floor
    version="v1",         # bump when logic changes
    api_key=None,         # reads ANTHROPIC_API_KEY if None
)
```

`canonical_hash` computation:

```python
import hashlib
rubric_hash = hashlib.sha256(rubric.encode()).hexdigest()
canonical_hash = hashlib.sha256(
    f"{class_name}:{version}:{model}:{rubric_hash}:{floor}".encode()
).hexdigest()
```

Any change to model, rubric, floor, or version produces a different hash.

`build_prompt`: formats the artifact text and rubric into a prompt asking
the LLM to output a JSON object with `{"score": 0.xx, "rationale": "..."}`.
The score is extracted and compared against `floor`.

Failure modes:

| Condition | Result | evidence.kind |
|---|---|---|
| LLM API error | `refunded` | `evaluator_error` |
| LLM output unparseable | `refunded` | `evaluator_error` |
| Score >= floor | `accepted` | `schema_pass_with_score` |
| Score < floor | `rejected` | `schema_pass_with_score` |

The `LLMRubricEvaluator` never raises exceptions. All failures return a clean
`EvaluationOutput` so the Oracle can always settle.

## (f) EvaluatorRegistry

`EvaluatorRegistry` maps evaluator DIDs to `(Ed25519PublicKey, canonical_hash)`
pairs. It mirrors `NodeRegistry` in shape but carries one additional field:
the `canonical_hash` that pins the evaluator's algorithm version.

```python
from pathlib import Path
from core.primitives.evaluator import EvaluatorRegistry
from core.primitives.identity import Ed25519Keypair

tmp = Path("/tmp/evaluators")
reg = EvaluatorRegistry(root=tmp)

kp = Ed25519Keypair.generate()
reg.register(
    "did:companyos:my-evaluator",
    kp.public_key,
    "a" * 64,  # canonical_hash
)

pubkey, hash_ = reg.get("did:companyos:my-evaluator")
assert hash_ == "a" * 64
```

Rebinding policy: registering a DID that is already present with a DIFFERENT
`canonical_hash` raises `ValueError`. A hash change means the evaluator's
algorithm has changed; an explicit revoke-and-re-register step is required so
counterparties can decide whether to re-sign their SLAs.
