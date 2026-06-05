"""Config parsing for the web UI and Postgres sections."""

from __future__ import annotations

from pathlib import Path

import pytest

from ecoflow_nut.config import load_config

_BASE = """
ecoflow:
  mac: "AA:BB:CC:DD:EE:FF"
  serial: "P231TEST"
"""


def _write(tmp_path: Path, extra: str = "") -> str:
    path = tmp_path / "config.yaml"
    path.write_text(_BASE + extra)
    return str(path)


def test_defaults_disable_web_and_postgres(tmp_path: Path) -> None:
    config = load_config(_write(tmp_path))
    assert config.web.enabled is False
    assert config.web.port == 8080
    assert config.postgres.enabled is False
    assert config.postgres.table == "ecoflow_samples"


def test_parses_web_and_postgres_sections(tmp_path: Path) -> None:
    extra = """
web:
  enabled: true
  port: 9000
  auth_token: "filetoken"
  require_auth_for_read: true
postgres:
  enabled: true
  dsn: "postgresql://u:p@db/eco"
  min_interval_seconds: 30
  retention_days: 7
"""
    config = load_config(_write(tmp_path, extra))
    assert config.web.enabled is True
    assert config.web.port == 9000
    assert config.web.auth_token == "filetoken"
    assert config.web.require_auth_for_read is True
    assert config.postgres.dsn == "postgresql://u:p@db/eco"
    assert config.postgres.min_interval_seconds == 30
    assert config.postgres.retention_days == 7


def test_env_overrides_secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    extra = """
web:
  auth_token: "filetoken"
postgres:
  dsn: "file-dsn"
"""
    monkeypatch.setenv("ECOFLOW_WEB_TOKEN", "envtoken")
    monkeypatch.setenv("ECOFLOW_PG_DSN", "env-dsn")
    config = load_config(_write(tmp_path, extra))
    assert config.web.auth_token == "envtoken"
    assert config.postgres.dsn == "env-dsn"
