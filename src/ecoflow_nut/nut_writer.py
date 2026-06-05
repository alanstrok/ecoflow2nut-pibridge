"""Translate DELTA 3 telemetry into NUT variables and a dummy-ups ``.dev`` file.

The NUT ``dummy-ups`` driver in "repeating" mode reads a file of ``name: value``
lines and republishes them. We rewrite that file every poll cycle so any NUT
client (Unraid, Synology, upsc, ...) sees fresh values served by ``upsd``.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from .config import NutConfig
from .delta3 import DeviceState

# Status flags
STATUS_ONLINE = "OL"
STATUS_ON_BATTERY = "OB"
STATUS_LOW_BATTERY = "LB"

# Runtime sentinel used when nothing is being drawn from the pack.
_RUNTIME_IDLE_SECONDS = 99999
_INVERTER_EFFICIENCY = 0.9


def derive_status(state: DeviceState, nut: NutConfig) -> str:
    """Derive ``ups.status`` from telemetry.

    * On line (``OL``) when AC input is present and meaningfully drawing power.
    * On battery + low (``OB LB``) when SoC drops below the low threshold.
    * Otherwise on battery (``OB``).
    """
    ac_watts = state.ac_input_watts or 0.0
    # The DELTA 3 does not include the AC-charger flag in every frame; until we
    # have seen it, infer AC presence from AC input watts.
    if state.ac_input_present is not None:
        ac_present = state.ac_input_present
    else:
        ac_present = ac_watts > nut.ac_input_present_min_watts
    soc = state.soc_percent if state.soc_percent is not None else 100.0

    if ac_present and ac_watts > nut.ac_input_present_min_watts:
        return STATUS_ONLINE
    if soc < nut.thresholds.low_battery_percent:
        return f"{STATUS_ON_BATTERY} {STATUS_LOW_BATTERY}"
    return STATUS_ON_BATTERY


def estimate_runtime_seconds(state: DeviceState, nut: NutConfig) -> int:
    """Estimate battery runtime in seconds from SoC and current AC output load."""
    soc = state.soc_percent if state.soc_percent is not None else 0.0
    remaining_wh = (soc / 100.0) * nut.battery_capacity_wh * _INVERTER_EFFICIENCY
    load = state.ac_output_watts or 0.0
    if load > 0:
        return int((remaining_wh / load) * 3600)
    return _RUNTIME_IDLE_SECONDS


def build_variables(state: DeviceState, nut: NutConfig) -> dict[str, str]:
    """Build the full ordered NUT variable mapping for the current state."""
    static = nut.static_values
    status = derive_status(state, nut)
    runtime = estimate_runtime_seconds(state, nut)
    load_watts = int(state.ac_output_watts or 0)
    # NUT defines ups.load as load percent of capacity, not watts. Derive it
    # from the AC output against the nominal real power; ups.realpower carries
    # the actual watts.
    load_percent = (
        int(round(load_watts / nut.realpower_nominal * 100))
        if nut.realpower_nominal
        else 0
    )

    variables: dict[str, str] = {
        "device.mfr": static.manufacturer,
        "device.model": static.model,
        "device.serial": static.serial,
        "device.type": "ups",
        "ups.mfr": static.manufacturer,
        "ups.model": static.model,
        "ups.serial": static.serial,
        "ups.status": status,
        "ups.load": str(load_percent),
        "ups.realpower": str(load_watts),
        "ups.realpower.nominal": str(nut.realpower_nominal),
        "battery.charge": str(int(state.soc_percent or 0)),
        "battery.charge.low": str(nut.thresholds.low_battery_percent),
        "battery.charge.warning": str(nut.battery_warning_percent),
        "battery.runtime": str(runtime),
        "battery.runtime.low": str(nut.battery_runtime_low_seconds),
        "battery.type": nut.battery_type,
        "battery.voltage.nominal": _fmt(nut.battery_voltage_nominal),
        "input.voltage": str(static.input_voltage),
        "input.frequency": str(static.input_frequency),
        "input.transfer.low": str(nut.input_transfer_low),
        "input.transfer.high": str(nut.input_transfer_high),
        "output.voltage": str(static.output_voltage),
        "output.frequency": str(static.output_frequency),
    }
    return variables


def _fmt(value: float) -> str:
    """Format a float without a trailing ``.0`` for whole numbers."""
    return str(int(value)) if float(value).is_integer() else str(value)


def render(variables: dict[str, str]) -> str:
    """Render variables to dummy-ups file content (``name: value`` lines)."""
    return "".join(f"{name}: {value}\n" for name, value in variables.items())


class NutWriter:
    """Writes the dummy-ups ``.dev`` file atomically each poll cycle."""

    def __init__(self, config: NutConfig) -> None:
        self._config = config
        self._path = Path(config.dev_file_path)

    @property
    def path(self) -> Path:
        return self._path

    def write(self, state: DeviceState) -> dict[str, str]:
        """Render the current state and atomically replace the ``.dev`` file."""
        variables = build_variables(state, self._config)
        content = render(variables)
        self._atomic_write(content)
        return variables

    def _atomic_write(self, content: str) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=self._path.parent, prefix=".ecoflow-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w") as handle:
                handle.write(content)
            # mkstemp creates 0600; the dummy-ups driver runs as a different
            # user (nut) and must be able to read the state file.
            os.chmod(tmp, 0o644)
            os.replace(tmp, self._path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
