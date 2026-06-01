"""Auto-shutdown state-machine tests."""

import pytest

from ecoflow_nut.autoshutdown import AutoShutdownController, ShutdownAction
from ecoflow_nut.config import AutoShutdownConfig


def _cfg(**kw) -> AutoShutdownConfig:
    base = dict(
        enabled=True,
        trigger_soc_percent=10,
        recover_soc_percent=15,
        grace_period_seconds=300,
    )
    base.update(kw)
    return AutoShutdownConfig(**base)


def test_disabled_never_acts():
    c = AutoShutdownController(AutoShutdownConfig(enabled=False))
    assert c.evaluate(0, 5, on_battery=True) is ShutdownAction.NONE
    assert c.evaluate(1000, 1, on_battery=True) is ShutdownAction.NONE


def test_no_action_above_trigger():
    c = AutoShutdownController(_cfg())
    assert c.evaluate(0, 50, on_battery=True) is ShutdownAction.NONE
    assert c.armed is False


def test_does_not_arm_on_ac():
    c = AutoShutdownController(_cfg())
    # Even at low SoC, do nothing while on line power.
    assert c.evaluate(0, 5, on_battery=False) is ShutdownAction.NONE
    assert c.armed is False


def test_arms_then_cuts_after_grace():
    c = AutoShutdownController(_cfg(grace_period_seconds=300))
    assert c.evaluate(0, 9, on_battery=True) is ShutdownAction.ARMED
    assert c.armed is True
    # Before the grace period elapses: no cut.
    assert c.evaluate(100, 8, on_battery=True) is ShutdownAction.NONE
    assert c.seconds_until_cut(100) == pytest.approx(200)
    # After the grace period: cut once.
    assert c.evaluate(300, 7, on_battery=True) is ShutdownAction.CUT
    assert c.triggered is True
    # Subsequent observations do not re-cut.
    assert c.evaluate(400, 6, on_battery=True) is ShutdownAction.NONE


def test_hysteresis_band_keeps_counting():
    c = AutoShutdownController(_cfg(grace_period_seconds=300))
    assert c.evaluate(0, 10, on_battery=True) is ShutdownAction.ARMED
    # SoC bounces to 12 (between trigger 10 and recover 15): stays armed.
    assert c.evaluate(150, 12, on_battery=True) is ShutdownAction.NONE
    assert c.armed is True
    assert c.evaluate(300, 12, on_battery=True) is ShutdownAction.CUT


def test_recovery_disarms_before_cut():
    c = AutoShutdownController(_cfg())
    assert c.evaluate(0, 9, on_battery=True) is ShutdownAction.ARMED
    # AC returns -> disarm.
    assert c.evaluate(50, 9, on_battery=False) is ShutdownAction.DISARMED
    assert c.armed is False
    # Can arm again later.
    assert c.evaluate(100, 9, on_battery=True) is ShutdownAction.ARMED


def test_recovery_by_soc_climb():
    c = AutoShutdownController(_cfg(recover_soc_percent=15))
    assert c.evaluate(0, 9, on_battery=True) is ShutdownAction.ARMED
    assert c.evaluate(60, 16, on_battery=True) is ShutdownAction.DISARMED


def test_restore_on_recovery_when_triggered():
    c = AutoShutdownController(_cfg(grace_period_seconds=0, restore_on_recovery=True))
    assert c.evaluate(0, 9, on_battery=True) is ShutdownAction.CUT
    # Mains returns after a cut -> emit RESTORE.
    assert c.evaluate(10, 9, on_battery=False) is ShutdownAction.RESTORE


def test_no_restore_when_disabled():
    c = AutoShutdownController(_cfg(grace_period_seconds=0, restore_on_recovery=False))
    assert c.evaluate(0, 9, on_battery=True) is ShutdownAction.CUT
    assert c.evaluate(10, 9, on_battery=False) is ShutdownAction.DISARMED
