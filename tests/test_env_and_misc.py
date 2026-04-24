"""Misc tests: env loading, slug helpers, board onboarding presence."""
from __future__ import annotations

import os
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Env loader
# ---------------------------------------------------------------------------
def test_env_loader_idempotent(monkeypatch, tmp_path: Path) -> None:
    """If a key already in os.environ, .env must NOT clobber it."""
    from core.env import load_env
    monkeypatch.setenv("FAKE_TEST_KEY", "preset_value")
    fake_env = tmp_path / ".env"
    fake_env.write_text("FAKE_TEST_KEY=should_not_win\n", encoding="utf-8")
    load_env(fake_env)
    assert os.environ["FAKE_TEST_KEY"] == "preset_value"


def test_env_loader_sets_missing_keys(monkeypatch, tmp_path: Path) -> None:
    from core.env import load_env
    monkeypatch.delenv("FAKE_NEW_KEY", raising=False)
    fake_env = tmp_path / ".env"
    fake_env.write_text("FAKE_NEW_KEY=hello\n", encoding="utf-8")
    load_env(fake_env)
    assert os.environ.get("FAKE_NEW_KEY") == "hello"


def test_env_loader_missing_file_returns_empty(tmp_path: Path) -> None:
    from core.env import load_env
    result = load_env(tmp_path / "no-such.env")
    assert result == {}


def test_validate_runtime_environment_passes_when_set(monkeypatch) -> None:
    from core.env import validate_runtime_environment
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake")
    validate_runtime_environment()


def test_validate_runtime_environment_raises_on_missing(monkeypatch) -> None:
    from core.env import MissingRequiredEnv, validate_runtime_environment
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(MissingRequiredEnv):
        validate_runtime_environment()


def test_validate_runtime_environment_raises_on_empty(monkeypatch) -> None:
    from core.env import MissingRequiredEnv, validate_runtime_environment
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    with pytest.raises(MissingRequiredEnv):
        validate_runtime_environment()


def test_validate_runtime_environment_custom_required_set(monkeypatch) -> None:
    from core.env import MissingRequiredEnv, validate_runtime_environment
    monkeypatch.delenv("FAKE_CUSTOM_VAR", raising=False)
    with pytest.raises(MissingRequiredEnv) as excinfo:
        validate_runtime_environment(required=("FAKE_CUSTOM_VAR",))
    assert "FAKE_CUSTOM_VAR" in str(excinfo.value)


def test_validate_runtime_environment_lists_all_missing(monkeypatch) -> None:
    from core.env import MissingRequiredEnv, validate_runtime_environment
    monkeypatch.delenv("FAKE_ONE", raising=False)
    monkeypatch.delenv("FAKE_TWO", raising=False)
    with pytest.raises(MissingRequiredEnv) as excinfo:
        validate_runtime_environment(required=("FAKE_ONE", "FAKE_TWO"))
    message = str(excinfo.value)
    assert "FAKE_ONE" in message
    assert "FAKE_TWO" in message


def test_env_loader_strips_quotes(monkeypatch, tmp_path: Path) -> None:
    from core.env import load_env
    monkeypatch.delenv("FAKE_QUOTED_KEY", raising=False)
    fake_env = tmp_path / ".env"
    fake_env.write_text('FAKE_QUOTED_KEY="hello world"\n', encoding="utf-8")
    load_env(fake_env)
    assert os.environ.get("FAKE_QUOTED_KEY") == "hello world"


# ---------------------------------------------------------------------------
# Board profile + onboarding presence (no LLM)
# ---------------------------------------------------------------------------
def test_old_press_board_profiles_exist(old_press_dir: Path) -> None:
    """Six board members each have a {role-lower}-profile.md at the board/ root."""
    board_dir = old_press_dir / "board"
    assert board_dir.exists(), "board/ directory missing"
    expected_members = {
        "strategist", "storyteller", "analyst",
        "builder", "contrarian", "knowledgeelicitor",
    }
    found = set()
    for child in board_dir.iterdir():
        if child.is_file() and child.name.endswith("-profile.md"):
            found.add(child.name.removesuffix("-profile.md"))
    missing = expected_members - found
    assert not missing, f"Board members missing profile.md: {missing}"


def test_old_press_board_onboarding_present(old_press_dir: Path) -> None:
    """board/onboarding.json should exist after onboarding has run."""
    onboarding = old_press_dir / "board" / "onboarding.json"
    assert onboarding.exists(), "board/onboarding.json missing — board not onboarded"


def test_old_press_board_meetings_dir_exists(old_press_dir: Path) -> None:
    md = old_press_dir / "board" / "meetings"
    assert md.exists(), "board/meetings/ should exist (board has run before)"


def test_old_press_decisions_dir_exists(old_press_dir: Path) -> None:
    assert (old_press_dir / "decisions").exists()


# ---------------------------------------------------------------------------
# Smoke tests for the engine modules — no LLM, just imports
# ---------------------------------------------------------------------------
def test_orchestrator_module_imports() -> None:
    import core.orchestrator  # noqa: F401


def test_board_module_imports() -> None:
    import core.board  # noqa: F401


def test_meeting_module_imports() -> None:
    import core.meeting  # noqa: F401


def test_onboarding_module_imports() -> None:
    import core.onboarding  # noqa: F401


def test_managers_base_module_imports() -> None:
    import core.managers.base  # noqa: F401


# ---------------------------------------------------------------------------
# API key sanity (CRITICAL — without this the demo run fails fast)
# ---------------------------------------------------------------------------
def test_api_key_loadable_via_env_loader() -> None:
    """After load_env runs, ANTHROPIC_API_KEY should be populated.

    This guards against a missing ~/.company-os/.env file silently producing
    a 401 inside an expensive demo run.
    """
    from core.env import load_env
    load_env()
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    assert key, "ANTHROPIC_API_KEY not set after load_env() — demo runs will fail"
    # sk-ant-... is the expected prefix; don't print the value
    assert key.startswith("sk-"), "API key in unexpected format"
