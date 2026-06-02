"""CLI tests that do not require BLE hardware."""

from click.testing import CliRunner

from ecoflow_nut.cli import cli


def _write_config(tmp_path) -> str:
    dev = tmp_path / "nut" / "ecoflow.dev"
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "ecoflow:\n"
        '  mac: "9C:9E:6E:74:02:D2"\n'
        '  serial: "P231ZE1APH4E1669"\n'
        "nut:\n"
        f'  dev_file_path: "{dev}"\n'
        "logging:\n"
        '  format: "console"\n'
    )
    return str(cfg)


def test_seed_writes_state_file(tmp_path):
    cfg = _write_config(tmp_path)
    result = CliRunner().invoke(cli, ["--config", cfg, "seed"])
    assert result.exit_code == 0, result.output
    dev = tmp_path / "nut" / "ecoflow.dev"
    content = dev.read_text()
    assert "battery.charge: 100" in content
    assert "ups.status: OL" in content


def test_help_lists_commands():
    result = CliRunner().invoke(cli, ["--help"])
    assert result.exit_code == 0
    for cmd in ("read", "run", "seed", "ac", "usb", "dc"):
        assert cmd in result.output
