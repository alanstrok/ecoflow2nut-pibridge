"""Manual control / inspection CLI for the EcoFlow NUT bridge."""

from __future__ import annotations

import asyncio
import json
import sys

import click
import structlog

from . import delta3
from .ble_client import EcoFlowBLE
from .config import Config, load_config
from .delta3 import DeviceState
from .main import configure_logging, run_daemon, seed_state
from .nut_writer import NutWriter, build_variables

log = structlog.get_logger(__name__)


async def _read_once(config: Config, timeout: float = 30.0) -> DeviceState:
    """Connect, wait for one complete telemetry frame, and return the state."""
    done = asyncio.Event()
    client = EcoFlowBLE(
        config.ecoflow,
        config.ble,
        on_state=lambda state: done.set() if state.is_complete else None,
    )
    await client.connect()
    try:
        await asyncio.wait_for(done.wait(), timeout=timeout)
    except TimeoutError:
        log.warning("cli.read_timeout", note="returning partial state")
    finally:
        await client.disconnect()
    return client.state


async def _send(config: Config, packet) -> None:
    client = EcoFlowBLE(config.ecoflow, config.ble)
    await client.connect()
    try:
        if not await client.wait_authenticated(timeout=30):
            raise click.ClickException("authentication timed out")
        await client.send_command_packet(packet)
        # Give the write a moment to flush before disconnecting.
        await asyncio.sleep(1.0)
    finally:
        await client.disconnect()


def _toggle(config: Config, builder, state: str) -> None:
    enabled = state.lower() in ("on", "true", "1", "enable", "enabled")
    packet = builder(enabled)
    asyncio.run(_send(config, packet))
    click.echo(f"sent: {'enable' if enabled else 'disable'} -> ok")


@click.group()
@click.option(
    "--config",
    "config_path",
    default="config.yaml",
    show_default=True,
    help="Path to the YAML config file.",
)
@click.pass_context
def cli(ctx: click.Context, config_path: str) -> None:
    """EcoFlow DELTA 3 NUT bridge."""
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config_path


@cli.command()
@click.pass_context
def read(ctx: click.Context) -> None:
    """Connect, read one telemetry frame, and dump state + NUT variables."""
    config = load_config(ctx.obj["config_path"])
    configure_logging(config.logging.level, config.logging.format)
    state = asyncio.run(_read_once(config))
    variables = build_variables(state, config.nut)
    click.echo(
        json.dumps(
            {
                "soc_percent": state.soc_percent,
                "ac_input_watts": state.ac_input_watts,
                "ac_output_watts": state.ac_output_watts,
                "ac_input_present": state.ac_input_present,
                "ac_output_on": state.ac_output_on,
                "nut": variables,
            },
            indent=2,
        )
    )


@cli.group()
@click.pass_context
def ac(ctx: click.Context) -> None:  # noqa: D401
    """Toggle AC output."""


@cli.group()
@click.pass_context
def usb(ctx: click.Context) -> None:  # noqa: D401
    """Toggle USB output."""


@cli.group()
@click.pass_context
def dc(ctx: click.Context) -> None:  # noqa: D401
    """Toggle 12V DC output."""


def _register_toggle(group: click.Group, builder) -> None:
    @group.command("on")
    @click.pass_context
    def _on(ctx: click.Context) -> None:
        config = load_config(ctx.obj["config_path"])
        configure_logging(config.logging.level, config.logging.format)
        _toggle(config, builder, "on")

    @group.command("off")
    @click.pass_context
    def _off(ctx: click.Context) -> None:
        config = load_config(ctx.obj["config_path"])
        configure_logging(config.logging.level, config.logging.format)
        _toggle(config, builder, "off")


_register_toggle(ac, delta3.set_ac_enabled_packet)
_register_toggle(usb, delta3.set_usb_enabled_packet)
_register_toggle(dc, delta3.set_dc_enabled_packet)


@cli.command()
@click.pass_context
def run(ctx: click.Context) -> None:
    """Run the bridge daemon (default deployment mode)."""
    sys.exit(run_daemon(ctx.obj["config_path"]))


@cli.command()
@click.pass_context
def seed(ctx: click.Context) -> None:
    """Write an initial placeholder NUT state file.

    Used as a systemd ExecStartPre so the dummy-ups driver has a file to read
    before the first BLE telemetry arrives (otherwise the driver fails at boot).
    """
    config = load_config(ctx.obj["config_path"])
    configure_logging(config.logging.level, config.logging.format)
    NutWriter(config.nut).write(seed_state())
    click.echo(f"seeded {config.nut.dev_file_path}")


def main() -> None:
    cli(obj={})


if __name__ == "__main__":  # pragma: no cover
    main()
