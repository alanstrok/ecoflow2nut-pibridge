"""YAML configuration loading with typed dataclasses and sane defaults."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass(slots=True)
class EcoflowConfig:
    mac: str
    serial: str
    model: str = "delta3"
    poll_interval_seconds: int = 5
    # encrypt_type and user_id are only needed for the encrypted (type-7/ECDH)
    # handshake. encrypt_type "auto" reads it from the BLE advertisement.
    encrypt_type: str | int = "auto"
    user_id: str = ""


@dataclass(slots=True)
class BleConfig:
    adapter: str = "hci0"
    connect_timeout_seconds: int = 30
    reconnect_backoff_max_seconds: int = 60
    scan_timeout_seconds: int = 30


@dataclass(slots=True)
class NutThresholds:
    low_battery_percent: int = 25
    critical_battery_percent: int = 10


@dataclass(slots=True)
class NutStaticValues:
    input_voltage: int = 230
    input_frequency: int = 50
    output_voltage: int = 230
    output_frequency: int = 50
    manufacturer: str = "EcoFlow"
    model: str = "DELTA 3"
    serial: str = ""


@dataclass(slots=True)
class NutConfig:
    device_name: str = "ecoflow"
    dev_file_path: str = "/var/run/nut/ecoflow.dev"
    battery_capacity_wh: int = 1024
    realpower_nominal: int = 1800
    battery_warning_percent: int = 35
    battery_runtime_low_seconds: int = 300
    battery_type: str = "LiFePO4"
    battery_voltage_nominal: float = 51.2
    input_transfer_low: int = 200
    input_transfer_high: int = 250
    ac_input_present_min_watts: int = 10
    thresholds: NutThresholds = field(default_factory=NutThresholds)
    static_values: NutStaticValues = field(default_factory=NutStaticValues)


@dataclass(slots=True)
class LoggingConfig:
    level: str = "INFO"
    format: str = "json"


@dataclass(slots=True)
class Config:
    ecoflow: EcoflowConfig
    ble: BleConfig = field(default_factory=BleConfig)
    nut: NutConfig = field(default_factory=NutConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)


def _filter(cls: type, data: dict[str, Any]) -> dict[str, Any]:
    """Keep only keys that are fields of ``cls`` to tolerate extra YAML keys."""
    names = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    return {k: v for k, v in data.items() if k in names}


def load_config(path: str | Path) -> Config:
    """Load and validate configuration from a YAML file."""
    raw = yaml.safe_load(Path(path).read_text()) or {}

    eco_raw = raw.get("ecoflow")
    if not eco_raw or not eco_raw.get("mac"):
        raise ValueError("config: 'ecoflow.mac' is required")
    ecoflow = EcoflowConfig(**_filter(EcoflowConfig, eco_raw))

    ble = BleConfig(**_filter(BleConfig, raw.get("ble", {})))

    nut_raw = dict(raw.get("nut", {}))
    thresholds = NutThresholds(**_filter(NutThresholds, nut_raw.pop("thresholds", {})))
    static_raw = _filter(NutStaticValues, nut_raw.pop("static_values", {}))
    static = NutStaticValues(**static_raw)
    if not static.serial:
        static.serial = ecoflow.serial
    nut = NutConfig(
        **_filter(NutConfig, nut_raw), thresholds=thresholds, static_values=static
    )

    logging_cfg = LoggingConfig(**_filter(LoggingConfig, raw.get("logging", {})))

    return Config(ecoflow=ecoflow, ble=ble, nut=nut, logging=logging_cfg)
