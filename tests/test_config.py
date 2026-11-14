from __future__ import annotations

from pathlib import Path

import pytest

from t2k2.config import DEMO_BASE_URL, LIVE_BASE_URL, ConfigError, load_settings

CREDS = {"T212_API_KEY": "k", "T212_API_SECRET": "s"}


def test_defaults() -> None:
    settings = load_settings(CREDS)
    assert settings.base_url == LIVE_BASE_URL
    assert settings.is_live
    assert settings.strategy_file == Path("config/strategy.yaml")
    assert settings.log_level == "INFO"


def test_demo_and_overrides() -> None:
    settings = load_settings(
        {
            **CREDS,
            "T212_BASE_URL": DEMO_BASE_URL + "/",
            "T2K2_STRATEGY_FILE": "my.yaml",
            "LOG_LEVEL": "debug",
        }
    )
    assert settings.base_url == DEMO_BASE_URL
    assert not settings.is_live
    assert settings.strategy_file == Path("my.yaml")
    assert settings.log_level == "DEBUG"


def test_missing_credentials() -> None:
    with pytest.raises(ConfigError, match="T212_API_SECRET"):
        load_settings({"T212_API_KEY": "k"})


def test_rejects_foreign_host() -> None:
    with pytest.raises(ConfigError, match="T212_BASE_URL"):
        load_settings({**CREDS, "T212_BASE_URL": "https://evil.example.com"})


def test_secrets_not_in_repr() -> None:
    env = {
        "T212_API_KEY": "fake-key-123",  # pragma: allowlist secret
        "T212_API_SECRET": "fake-secret-456",  # pragma: allowlist secret
    }
    text = repr(load_settings(env))
    assert "fake-key-123" not in text
    assert "fake-secret-456" not in text
