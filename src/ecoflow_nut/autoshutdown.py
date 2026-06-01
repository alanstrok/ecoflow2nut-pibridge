"""Auto-shutdown decision logic.

A small, side-effect-free state machine that decides when to cut the DELTA 3's
output based on state of charge. Kept separate from the daemon and the BLE layer
so the policy can be unit-tested deterministically.

Flow (when enabled):

1. ``IDLE``  -> while on battery and SoC drops to ``trigger_soc_percent`` the
   controller arms and starts a grace countdown (``ARMED``). The grace period
   lets NUT clients finish their own ``OB LB`` shutdown first.
2. ``ARMED`` -> once ``grace_period_seconds`` elapses while still critical, it
   emits ``CUT`` once (``TRIGGERED``).
3. Recovery (AC returns, or SoC climbs back to ``recover_soc_percent``) disarms
   the controller (``DISARMED``); if it had already cut and
   ``restore_on_recovery`` is set, it emits ``RESTORE`` to turn output back on.
"""

from __future__ import annotations

from enum import Enum

from .config import AutoShutdownConfig


class ShutdownAction(Enum):
    NONE = "none"
    ARMED = "armed"
    DISARMED = "disarmed"
    CUT = "cut"
    RESTORE = "restore"


class AutoShutdownController:
    """Decides shutdown actions from successive (soc, on_battery) observations."""

    def __init__(self, config: AutoShutdownConfig) -> None:
        self._config = config
        self._armed_at: float | None = None
        self._triggered = False

    @property
    def armed(self) -> bool:
        return self._armed_at is not None

    @property
    def triggered(self) -> bool:
        return self._triggered

    def seconds_until_cut(self, now: float) -> float | None:
        """Remaining grace seconds, or None when not currently armed."""
        if self._armed_at is None or self._triggered:
            return None
        return max(0.0, self._config.grace_period_seconds - (now - self._armed_at))

    def evaluate(self, now: float, soc: float | None, on_battery: bool) -> ShutdownAction:
        """Advance the state machine for one observation and return any action."""
        if not self._config.enabled:
            return ShutdownAction.NONE

        recovered = (not on_battery) or (
            soc is not None and soc >= self._config.recover_soc_percent
        )

        if recovered:
            was_triggered = self._triggered
            was_active = self._armed_at is not None or self._triggered
            self._armed_at = None
            self._triggered = False
            if was_triggered and self._config.restore_on_recovery:
                return ShutdownAction.RESTORE
            if was_active:
                return ShutdownAction.DISARMED
            return ShutdownAction.NONE

        if self._triggered:
            return ShutdownAction.NONE

        critical = (
            on_battery and soc is not None and soc <= self._config.trigger_soc_percent
        )

        just_armed = False
        if self._armed_at is None:
            # Arm only when SoC actually drops to the trigger level.
            if not critical:
                return ShutdownAction.NONE
            self._armed_at = now
            just_armed = True

        # Once armed, the grace countdown runs until recovery -- including while
        # SoC sits in the hysteresis band between trigger and recover.
        if now - self._armed_at >= self._config.grace_period_seconds:
            self._triggered = True
            return ShutdownAction.CUT
        return ShutdownAction.ARMED if just_armed else ShutdownAction.NONE
