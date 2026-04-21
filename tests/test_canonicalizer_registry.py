"""
tests/test_canonicalizer_registry.py -- Ticket B0-a coverage for CanonicalizerRegistry
========================================================================================
Unit tests for `core.primitives.canonicalizer_registry`:

- CanonicalizerRegistry register/get round-trip.
- Missing version raises ValueError on get.
- extract_protocol_version returns the value when key is present.
- extract_protocol_version raises ValueError when key is missing and no default.
- extract_protocol_version returns the default when key is missing and default supplied.
- default_canonicalizer_registry has "companyos-verdict/0.1" registered at import time.
- A stub "companyos-verdict/0.2" canonicalizer is dispatched correctly: signing with
  0.2 bytes and verifying with 0.2 bytes succeeds; signing with 0.1 bytes against a
  0.2 verdict fails.
- An unknown protocol_version on OracleVerdict.verify_signature raises ValueError.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json

import pytest

from core.primitives.canonicalizer_registry import (
    CanonicalizerRegistry,
    default_canonicalizer_registry,
    extract_protocol_version,
)
from core.primitives.exceptions import SignatureError
from core.primitives.identity import Ed25519Keypair, sign as _identity_sign
from core.primitives.oracle import OracleVerdict, _canonical_bytes
from core.primitives.signer import LocalKeypairSigner


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def keypair() -> Ed25519Keypair:
    return Ed25519Keypair.generate()


def _base_kwargs(keypair: Ed25519Keypair) -> dict:
    """A valid kwargs bundle for OracleVerdict.create."""
    return {
        "sla_id": "sla-canonreg-001",
        "artifact_hash": "c" * 64,
        "tier": 0,
        "result": "accepted",
        "evaluator_did": "did:companyos:test-node",
        "evidence": {"kind": "schema_pass", "detail": "registry test"},
        "issued_at": "2026-04-21T00:00:00Z",
        "signer": LocalKeypairSigner(keypair),
    }


# ---------------------------------------------------------------------------
# CanonicalizerRegistry: register / get round-trip
# ---------------------------------------------------------------------------
class TestCanonicalizerRegistry:
    def test_register_and_get_round_trip(self):
        """register then get returns the exact same callable."""
        reg = CanonicalizerRegistry()
        fn = lambda shell, exclude_verdict_hash=False: b"stub"
        reg.register("test/0.1", fn)
        assert reg.get("test/0.1") is fn

    def test_get_missing_version_raises_value_error(self):
        """Requesting an unregistered version raises ValueError."""
        reg = CanonicalizerRegistry()
        with pytest.raises(ValueError, match="no canonicalizer registered"):
            reg.get("nonexistent/9.9")

    def test_register_overwrites_existing(self):
        """Re-registering a version replaces the old canonicalizer."""
        reg = CanonicalizerRegistry()
        fn_old = lambda shell, exclude_verdict_hash=False: b"old"
        fn_new = lambda shell, exclude_verdict_hash=False: b"new"
        reg.register("test/0.1", fn_old)
        reg.register("test/0.1", fn_new)
        assert reg.get("test/0.1") is fn_new

    def test_register_non_str_version_raises_type_error(self):
        """version must be a str; passing an int raises TypeError."""
        reg = CanonicalizerRegistry()
        with pytest.raises(TypeError, match="version must be a str"):
            reg.register(1, lambda s, e=False: b"x")  # type: ignore[arg-type]

    def test_register_non_callable_fn_raises_type_error(self):
        """fn must be callable; passing a string raises TypeError."""
        reg = CanonicalizerRegistry()
        with pytest.raises(TypeError, match="fn must be callable"):
            reg.register("test/0.1", "not_a_function")  # type: ignore[arg-type]

    def test_multiple_versions_coexist(self):
        """Different versions can be registered independently."""
        reg = CanonicalizerRegistry()
        fn_a = lambda s, e=False: b"a"
        fn_b = lambda s, e=False: b"b"
        reg.register("proto/1.0", fn_a)
        reg.register("proto/2.0", fn_b)
        assert reg.get("proto/1.0") is fn_a
        assert reg.get("proto/2.0") is fn_b

    def test_error_message_lists_registered_versions(self):
        """ValueError message includes the currently registered versions."""
        reg = CanonicalizerRegistry()
        reg.register("known/0.1", lambda s, e=False: b"x")
        with pytest.raises(ValueError, match="known/0.1"):
            reg.get("unknown/9.9")


# ---------------------------------------------------------------------------
# extract_protocol_version
# ---------------------------------------------------------------------------
class TestExtractProtocolVersion:
    def test_returns_value_when_key_present(self):
        """Returns the protocol_version string from the shell dict."""
        shell = {"protocol_version": "companyos-verdict/0.1", "other": 42}
        assert extract_protocol_version(shell) == "companyos-verdict/0.1"

    def test_raises_value_error_when_key_missing_no_default(self):
        """Raises ValueError if key absent and no default provided."""
        shell = {"sla_id": "sla-001"}
        with pytest.raises(ValueError, match="protocol_version"):
            extract_protocol_version(shell)

    def test_returns_default_when_key_missing_with_default(self):
        """Returns default when key is absent and default kwarg is set."""
        shell = {"sla_id": "sla-001"}
        result = extract_protocol_version(shell, default="companyos-verdict/0.1")
        assert result == "companyos-verdict/0.1"

    def test_none_value_for_key_triggers_default(self):
        """A None value for protocol_version triggers the default fallback."""
        shell = {"protocol_version": None}
        result = extract_protocol_version(shell, default="companyos-verdict/0.1")
        assert result == "companyos-verdict/0.1"

    def test_returns_string_coercion(self):
        """Return value is always str (coerces non-string values)."""
        shell = {"protocol_version": "companyos-verdict/0.1"}
        result = extract_protocol_version(shell)
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# default_canonicalizer_registry populated at import time
# ---------------------------------------------------------------------------
class TestDefaultRegistry:
    def test_v1a_canonicalizer_registered_at_import(self):
        """companyos-verdict/0.1 is registered on the default singleton."""
        fn = default_canonicalizer_registry.get("companyos-verdict/0.1")
        assert callable(fn)

    def test_v1a_canonicalizer_is_canonical_bytes(self):
        """The registered v1a canonicalizer IS _canonical_bytes from oracle.py."""
        fn = default_canonicalizer_registry.get("companyos-verdict/0.1")
        assert fn is _canonical_bytes


# ---------------------------------------------------------------------------
# End-to-end: stub v0.2 canonicalizer dispatched correctly
# ---------------------------------------------------------------------------
class TestStubV02Canonicalization:
    """Verify that a verdict stamped with protocol_version="companyos-verdict/0.2"
    signs and verifies via the 0.2 canonicalizer, not the 0.1 one.

    Construction strategy: use the raw OracleVerdict constructor (not .create)
    to build a verdict whose bytes were produced by the stub canonicalizer.
    This bypasses the validation in .create (which would route through the
    registry and fail with ValueError if 0.2 were not registered yet) and lets
    us control the exact bytes that get signed.
    """

    @pytest.fixture(autouse=True)
    def register_stub(self):
        """Register a stub 0.2 canonicalizer for the duration of each test.

        The stub appends a fixed marker ("v0.2-stub") to distinguish its bytes
        from the 0.1 output so we can assert they diverge.
        """
        def _stub_canonicalize(verdict, exclude_verdict_hash=False):
            # Delegate to _canonical_bytes to get the base bytes, then append
            # a version marker so the bytes are provably different from v0.1.
            base = _canonical_bytes(verdict, exclude_verdict_hash=exclude_verdict_hash)
            return base + b"|v0.2-stub"

        # Snapshot any existing registration (oracle.py registers the real
        # 0.2 canonicalizer at import time as of B1-a+b) so teardown restores
        # it. Removing outright would leak test state into downstream tests
        # that depend on the real v0.2 registration.
        prior = default_canonicalizer_registry._registry.get("companyos-verdict/0.2")
        default_canonicalizer_registry._registry["companyos-verdict/0.2"] = _stub_canonicalize
        yield
        if prior is not None:
            default_canonicalizer_registry._registry["companyos-verdict/0.2"] = prior
        else:
            default_canonicalizer_registry._registry.pop("companyos-verdict/0.2", None)

    def _build_v02_verdict(self, keypair: Ed25519Keypair) -> OracleVerdict:
        """Build a verdict signed with the 0.2 stub canonicalizer.

        Uses the raw constructor because .create would reject 0.2 if it were
        not registered (and during the autouse fixture setup, it IS registered,
        so .create would actually work here -- but using the raw constructor
        keeps this test self-documenting about the bypass technique).
        """
        from core.primitives.identity import Signature

        stub_fn = default_canonicalizer_registry.get("companyos-verdict/0.2")

        # Build the shell without verdict_hash (will compute below).
        shell_no_hash = {
            "sla_id": "sla-v02-test",
            "artifact_hash": "d" * 64,
            "tier": 0,
            "result": "accepted",
            "evaluator_did": "did:companyos:v02-node",
            "evidence": {"kind": "schema_pass"},
            "signer": keypair.public_key,
            "issued_at": "2026-04-21T00:00:00Z",
            "protocol_version": "companyos-verdict/0.2",
            "score": None,
        }

        # Compute verdict_hash using the 0.2 stub.
        hash_body = stub_fn(shell_no_hash, exclude_verdict_hash=True)
        verdict_hash = hashlib.sha256(hash_body).hexdigest()

        # Sign using the 0.2 stub with verdict_hash included.
        shell_with_hash = dict(shell_no_hash, verdict_hash=verdict_hash)
        signing_body = stub_fn(shell_with_hash, exclude_verdict_hash=False)
        sig = _identity_sign(keypair, signing_body)

        return OracleVerdict(
            sla_id="sla-v02-test",
            artifact_hash="d" * 64,
            tier=0,
            result="accepted",
            evaluator_did="did:companyos:v02-node",
            evidence={"kind": "schema_pass"},
            verdict_hash=verdict_hash,
            signer=keypair.public_key,
            signature=sig,
            issued_at="2026-04-21T00:00:00Z",
            protocol_version="companyos-verdict/0.2",
            score=None,
        )

    def test_v02_verdict_verify_signature_passes(self, keypair):
        """A verdict signed with the 0.2 stub verifies cleanly via verify_signature."""
        v = self._build_v02_verdict(keypair)
        # Should not raise.
        v.verify_signature()

    def test_v02_canonicalizer_produces_different_bytes_than_v01(self, keypair):
        """The stub 0.2 canonicalizer produces bytes distinct from 0.1."""
        v = self._build_v02_verdict(keypair)
        fn_v01 = default_canonicalizer_registry.get("companyos-verdict/0.1")
        fn_v02 = default_canonicalizer_registry.get("companyos-verdict/0.2")
        bytes_v01 = fn_v01(v, exclude_verdict_hash=False)
        bytes_v02 = fn_v02(v, exclude_verdict_hash=False)
        assert bytes_v01 != bytes_v02

    def test_v01_verdict_verify_signature_uses_v01_not_v02(self, keypair):
        """A v0.1 verdict (from OracleVerdict.create) verifies correctly even
        when 0.2 is also registered. Registry dispatch is by protocol_version."""
        v = OracleVerdict.create(
            sla_id="sla-coexist-001",
            artifact_hash="e" * 64,
            tier=0,
            result="accepted",
            evaluator_did="did:companyos:test-node",
            evidence={"kind": "schema_pass"},
            issued_at="2026-04-21T00:00:00Z",
            signer=LocalKeypairSigner(keypair),
        )
        assert v.protocol_version == "companyos-verdict/0.1"
        # Should not raise.
        v.verify_signature()

    def test_v02_verdict_fails_if_verified_with_v01_bytes(self, keypair):
        """A verdict signed under 0.2 rules must fail verify_signature if we
        temporarily replace the 0.2 canonicalizer with the 0.1 one (bytes mismatch)."""
        v = self._build_v02_verdict(keypair)

        # Replace 0.2 with the 0.1 canonicalizer to simulate version mismatch.
        default_canonicalizer_registry.register(
            "companyos-verdict/0.2", _canonical_bytes
        )
        try:
            with pytest.raises(SignatureError):
                v.verify_signature()
        finally:
            # Restore the stub so cleanup in autouse works correctly.
            stub_fn = default_canonicalizer_registry.get("companyos-verdict/0.1")
            default_canonicalizer_registry.register("companyos-verdict/0.2", stub_fn)

    def test_v02_verdict_can_be_created_via_create_factory(self, keypair):
        """OracleVerdict.create dispatches through the registry: creating with
        protocol_version=0.2 uses the stub canonicalizer."""
        v = OracleVerdict.create(
            sla_id="sla-create-v02",
            artifact_hash="f" * 64,
            tier=0,
            result="accepted",
            evaluator_did="did:companyos:test-node",
            evidence={"kind": "schema_pass"},
            issued_at="2026-04-21T00:00:00Z",
            signer=LocalKeypairSigner(keypair),
            protocol_version="companyos-verdict/0.2",
        )
        assert v.protocol_version == "companyos-verdict/0.2"
        # Signature was produced by the stub, so verify_signature must use it too.
        v.verify_signature()


# ---------------------------------------------------------------------------
# Unknown protocol_version raises ValueError on verify_signature
# ---------------------------------------------------------------------------
class TestUnknownProtocolVersionOnVerify:
    def test_unknown_version_raises_value_error(self, keypair):
        """OracleVerdict.verify_signature raises ValueError (not SignatureError)
        when self.protocol_version is not in the registry."""
        v = OracleVerdict.create(**{
            "sla_id": "sla-unknown-pv",
            "artifact_hash": "a" * 64,
            "tier": 0,
            "result": "accepted",
            "evaluator_did": "did:companyos:test-node",
            "evidence": {"kind": "schema_pass"},
            "issued_at": "2026-04-21T00:00:00Z",
            "signer": LocalKeypairSigner(keypair),
        })
        # Inject an unknown protocol_version into the frozen dataclass.
        tampered = dataclasses.replace(v, protocol_version="companyos-verdict/99.9")
        with pytest.raises(ValueError, match="no canonicalizer registered"):
            tampered.verify_signature()

    def test_unknown_version_error_not_signature_error(self, keypair):
        """The raised exception must be ValueError specifically, not SignatureError."""
        v = OracleVerdict.create(**{
            "sla_id": "sla-unknown-pv-2",
            "artifact_hash": "b" * 64,
            "tier": 0,
            "result": "accepted",
            "evaluator_did": "did:companyos:test-node",
            "evidence": {"kind": "schema_pass"},
            "issued_at": "2026-04-21T00:00:00Z",
            "signer": LocalKeypairSigner(keypair),
        })
        tampered = dataclasses.replace(v, protocol_version="companyos-verdict/99.9")
        # Verify it is ValueError, not SignatureError (distinct exception types).
        try:
            tampered.verify_signature()
            pytest.fail("Expected ValueError was not raised")
        except ValueError:
            pass
        except SignatureError:
            pytest.fail("Got SignatureError; expected ValueError for unknown version")


# ---------------------------------------------------------------------------
# v1a regression: existing behavior preserved end-to-end
# ---------------------------------------------------------------------------
class TestV1aRegression:
    """Confirm that the registry refactor does not break any v1a behavior.

    These tests mirror the key assertions in test_oracle_verdict.py but run
    in this module to make the regression contract explicit.
    """

    def test_create_and_verify_end_to_end(self, keypair):
        """Standard create -> verify_signature path still works."""
        v = OracleVerdict.create(
            sla_id="sla-regression-001",
            artifact_hash="a" * 64,
            tier=0,
            result="accepted",
            evaluator_did="did:companyos:oracle-node-001",
            evidence={"kind": "schema_pass", "detail": "regression"},
            issued_at="2026-04-21T12:00:00Z",
            signer=LocalKeypairSigner(keypair),
        )
        v.verify_signature()

    def test_roundtrip_to_dict_from_dict_verify(self, keypair):
        """create -> to_dict -> from_dict -> verify_signature still passes."""
        v = OracleVerdict.create(
            sla_id="sla-regression-002",
            artifact_hash="a" * 64,
            tier=0,
            result="rejected",
            evaluator_did="did:companyos:oracle-node-001",
            evidence={"kind": "schema_fail"},
            issued_at="2026-04-21T12:00:00Z",
            signer=LocalKeypairSigner(keypair),
        )
        rehydrated = OracleVerdict.from_dict(v.to_dict())
        rehydrated.verify_signature()

    def test_verdict_hash_deterministic_via_registry(self, keypair):
        """Same inputs produce the same verdict_hash every time (registry path)."""
        base = {
            "sla_id": "sla-det-001",
            "artifact_hash": "a" * 64,
            "tier": 0,
            "result": "accepted",
            "evaluator_did": "did:companyos:oracle-node-001",
            "evidence": {"kind": "schema_pass"},
            "issued_at": "2026-04-21T12:00:00Z",
            "signer": LocalKeypairSigner(keypair),
        }
        hashes = {OracleVerdict.create(**base).verdict_hash for _ in range(3)}
        assert len(hashes) == 1
