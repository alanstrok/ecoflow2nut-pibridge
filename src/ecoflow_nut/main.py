"""Daemon: poll the DELTA 3 over BLE and keep the NUT dummy-ups file fresh."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
import time

import structlog

from . import delta3
from .autoshutdown import AutoShutdownController, ShutdownAction
from .ble_client import EcoFlowBLE
from .config import Config, load_config
from .delta3 import DeviceState
from .nut_writer import NutWriter

log = structlog.get_logger(__name__)

# If no successful BLE read happens within this window, exit so the supervisor
# (systemd / Docker) restarts us from a clean state.
WATCHDOG_TIMEOUT_SECONDS = 120


def configure_logging(level: str, fmt: str) -> None:
    """Configure structlog for JSON or console output."""
    renderer = (
        structlog.processors.JSONRenderer()
        if fmt == "json"
        else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
    )


class Daemon:
    """Owns the BLE connection lifecycle, the poll loop and the watchdog."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._writer = NutWriter(config.nut)
        self._stop = asyncio.Event()
        self._last_write_monotonic = time.monotonic()
        self._autoshutdown = AutoShutdownController(config.auto_shutdown)
        self._active_client: EcoFlowBLE | None = None

    def request_stop(self, *_: object) -> None:
        log.info("daemon.stop_requested")
        self._stop.set()

    async def run(self) -> int:
        # Seed the NUT file immediately so clients have something to read while
        # we establish the first BLE connection.
        self._writer.write(DeviceState(soc_percent=100, ac_input_present=True))

        watchdog = asyncio.create_task(self._watchdog())
        try:
            backoff = 1.0
            while not self._stop.is_set():
                client = EcoFlowBLE(
                    self._config.ecoflow,
                    self._config.ble,
                    on_state=self._on_state,
                )
                try:
                    await client.connect()
                    if not await client.wait_authenticated(timeout=30):
                        raise TimeoutError("authentication/first-read timed out")
                    log.info("daemon.connected")
                    backoff = 1.0
                    self._active_client = client
                    await self._poll_loop(client)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    log.error(
                        "daemon.connection_error",
                        error=str(exc),
                        error_type=type(exc).__name__,
                        error_repr=repr(exc),
                    )
                finally:
                    self._active_client = None
                    await client.disconnect()

                if self._stop.is_set():
                    break
                backoff = min(backoff * 2, self._config.ble.reconnect_backoff_max_seconds)
                log.info("daemon.reconnect_wait", seconds=round(backoff, 1))
                await self._sleep_or_stop(backoff)
        finally:
            watchdog.cancel()
        return 0

    async def _poll_loop(self, client: EcoFlowBLE) -> None:
        interval = self._config.ecoflow.poll_interval_seconds
        while not self._stop.is_set():
            if not client.is_connected:
                log.warning("daemon.poll_lost_connection")
                return
            await self._sleep_or_stop(interval)

    def _on_state(self, state: DeviceState) -> None:
        if not state.is_complete:
            return
        variables = self._writer.write(state)
        self._last_write_monotonic = time.monotonic()
        status = variables.get("ups.status", "OB")
        log.info(
            "state.updated",
            soc=state.soc_percent,
            ac_in=state.ac_input_watts,
            ac_out=state.ac_output_watts,
            status=status,
        )

        on_battery = status != "OL"
        action = self._autoshutdown.evaluate(
            time.monotonic(),
            state.soc_percent,
            on_battery,
            state.ac_output_watts,
        )
        if action is not ShutdownAction.NONE:
            self._handle_shutdown_action(action)

    def _handle_shutdown_action(self, action: ShutdownAction) -> None:
        cfg = self._config.auto_shutdown
        if action is ShutdownAction.ARMED:
            log.warning(
                "auto_shutdown.armed",
                trigger_soc=cfg.trigger_soc_percent,
                grace_seconds=cfg.grace_period_seconds,
            )
        elif action is ShutdownAction.DISARMED:
            log.info("auto_shutdown.disarmed")
        elif action is ShutdownAction.CUT:
            log.critical("auto_shutdown.cut", outputs=self._cut_outputs())
            self._send_outputs(enabled=False)
        elif action is ShutdownAction.RESTORE:
            log.warning("auto_shutdown.restore", outputs=self._cut_outputs())
            self._send_outputs(enabled=True)

    def _cut_outputs(self) -> list[str]:
        cfg = self._config.auto_shutdown
        names = []
        if cfg.cut_ac:
            names.append("ac")
        if cfg.cut_usb:
            names.append("usb")
        if cfg.cut_dc:
            names.append("dc")
        return names

    def _send_outputs(self, enabled: bool) -> None:
        cfg = self._config.auto_shutdown
        client = self._active_client
        if client is None:
            log.error("auto_shutdown.no_client", note="cannot send output command")
            return
        packets = []
        if cfg.cut_ac:
            packets.append(delta3.set_ac_enabled_packet(enabled))
        if cfg.cut_usb:
            packets.append(delta3.set_usb_enabled_packet(enabled))
        if cfg.cut_dc:
            packets.append(delta3.set_dc_enabled_packet(enabled))

        async def _send() -> None:
            for packet in packets:
                try:
                    await client.send_command_packet(packet)
                except Exception as exc:  # noqa: BLE001
                    log.error("auto_shutdown.send_failed", error=str(exc))
                await asyncio.sleep(0.3)

        asyncio.create_task(_send())

    async def _watchdog(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(5)
            stale = time.monotonic() - self._last_write_monotonic
            if stale > WATCHDOG_TIMEOUT_SECONDS:
                log.critical("daemon.watchdog_timeout", stale_seconds=round(stale))
                self._stop.set()
                # Hard-exit so the supervisor restarts us.
                sys.exit(70)

    async def _sleep_or_stop(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except TimeoutError:
            pass


async def _amain(config: Config) -> int:
    daemon = Daemon(config)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, daemon.request_stop)
        except NotImplementedError:  # pragma: no cover - non-unix
            signal.signal(sig, daemon.request_stop)
    return await daemon.run()


def run_daemon(config_path: str) -> int:
    """Synchronous entry point used by the CLI ``run`` command."""
    config = load_config(config_path)
    configure_logging(config.logging.level, config.logging.format)
    log.info("daemon.starting", mac=config.ecoflow.mac, model=config.ecoflow.model)
    return asyncio.run(_amain(config))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(run_daemon(sys.argv[1] if len(sys.argv) > 1 else "config.yaml"))
