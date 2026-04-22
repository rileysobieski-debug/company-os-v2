"""Pre-kernel harness: model sovereignty (Anthropic <-> Llama 4 swap).

Week 1 plan item. Target: `core.llm_client` routed through an MCP
adapter so the default backend can switch from Anthropic to a local
Llama 4 in under 5 minutes with no code change beyond config.

Expected state:

  - With the current Phase 1 code path: this harness xfails because
    `llm_client.set_backend()` does not exist yet.
  - After Weeks 6-7 (MCP adapter + model-swap verification): the
    xfail markers flip to pass, and running this file is the
    sovereignty audit.
"""
from __future__ import annotations

import pytest


@pytest.mark.xfail(
    reason="MCP backend selection is a Weeks 6-7 deliverable",
    strict=True,
    raises=(AttributeError, ImportError, NotImplementedError, AssertionError),
)
def test_backend_swap_api_exists():
    from core import llm_client  # noqa: F401
    # Must expose a `set_backend(name)` and `get_backend()` pair.
    assert hasattr(llm_client, "set_backend")
    assert hasattr(llm_client, "get_backend")


@pytest.mark.xfail(
    reason="MCP backend selection is a Weeks 6-7 deliverable",
    strict=True,
    raises=(AttributeError, ImportError, NotImplementedError, AssertionError),
)
def test_anthropic_to_llama_roundtrip():
    from core import llm_client
    llm_client.set_backend("anthropic")
    assert llm_client.get_backend() == "anthropic"
    llm_client.set_backend("llama4-local")
    assert llm_client.get_backend() == "llama4-local"


@pytest.mark.xfail(
    reason="Output-shape parity across backends is a Weeks 6-7 deliverable",
    strict=True,
    raises=(AttributeError, ImportError, NotImplementedError, AssertionError),
)
def test_functional_output_shape_invariant():
    """The same prompt through two backends must produce responses
    with identical structural shape (fields, types). Content may
    differ; shape may not."""
    from core import llm_client
    llm_client.set_backend("anthropic")
    resp_a = llm_client.single_turn("ping", model="adapter-default")
    llm_client.set_backend("llama4-local")
    resp_b = llm_client.single_turn("ping", model="adapter-default")
    assert type(resp_a) is type(resp_b)
    assert set(resp_a.keys()) == set(resp_b.keys())
