#!/bin/sh
# Container entrypoint: bring up Bluetooth (BlueZ), the NUT server (dummy-ups
# driver + upsd) and the EcoFlow BLE bridge daemon together. A simple shell
# supervisor -- if the bridge exits, we tear everything down so Docker restarts
# the container.
set -eu

CONFIG="${ECOFLOW_CONFIG:-/app/config/config.yaml}"
NUT_RUN_DIR="/var/run/nut"
DEV_FILE="${NUT_DEV_FILE:-${NUT_RUN_DIR}/ecoflow.dev}"
# Set ECOFLOW_USE_HOST_DBUS=1 to use a bind-mounted host D-Bus/bluetoothd
# instead of starting BlueZ inside the container.
USE_HOST_DBUS="${ECOFLOW_USE_HOST_DBUS:-0}"

DBUS_PID=""
BLUETOOTHD_PID=""
BRIDGE_PID=""

mkdir -p "${NUT_RUN_DIR}"
chown -R nut:nut "${NUT_RUN_DIR}" 2>/dev/null || true

cleanup() {
    echo "[entrypoint] shutting down..."
    [ -n "${BRIDGE_PID}" ] && kill "${BRIDGE_PID}" 2>/dev/null || true
    upsd -c stop 2>/dev/null || true
    upsdrvctl stop 2>/dev/null || true
    [ -n "${BLUETOOTHD_PID}" ] && kill "${BLUETOOTHD_PID}" 2>/dev/null || true
    [ -n "${DBUS_PID}" ] && kill "${DBUS_PID}" 2>/dev/null || true
    exit 0
}
trap cleanup TERM INT

find_bluetoothd() {
    for p in /usr/libexec/bluetooth/bluetoothd \
             /usr/lib/bluetooth/bluetoothd \
             /usr/sbin/bluetoothd; do
        [ -x "$p" ] && { echo "$p"; return 0; }
    done
    command -v bluetoothd 2>/dev/null && return 0
    return 1
}

start_bluetooth() {
    if [ "${USE_HOST_DBUS}" = "1" ]; then
        echo "[entrypoint] ECOFLOW_USE_HOST_DBUS=1: using host D-Bus / bluetoothd"
        return 0
    fi

    echo "[entrypoint] starting internal D-Bus + bluetoothd..."
    mkdir -p /run/dbus
    rm -f /run/dbus/pid
    dbus-daemon --system --fork
    DBUS_PID="$(cat /run/dbus/pid 2>/dev/null || pgrep -n dbus-daemon || true)"

    btd="$(find_bluetoothd || true)"
    if [ -z "${btd}" ]; then
        echo "[entrypoint] ERROR: bluetoothd binary not found" >&2
        cleanup
    fi
    "${btd}" --experimental &
    BLUETOOTHD_PID=$!

    # Wait for the kernel adapter (hci0) to be registered with BlueZ, then power
    # it on. bleak/BlueZ will not scan on a powered-off adapter.
    echo "[entrypoint] waiting for a Bluetooth adapter..."
    i=0
    while ! bluetoothctl list 2>/dev/null | grep -qi "Controller"; do
        sleep 1
        i=$((i + 1))
        if [ "${i}" -ge 20 ]; then
            echo "[entrypoint] WARNING: no Bluetooth controller visible after 20s." >&2
            echo "[entrypoint] If running in 'bridge' network mode, switch the" >&2
            echo "[entrypoint] container Network Type to 'host' -- the kernel only" >&2
            echo "[entrypoint] exposes hci0 in the host network namespace." >&2
            break
        fi
    done
    bluetoothctl power on 2>/dev/null || true
    echo "[entrypoint] bluetooth status:"
    bluetoothctl list 2>/dev/null || true
}

start_bluetooth

echo "[entrypoint] starting EcoFlow bridge daemon..."
ecoflow-nut --config "${CONFIG}" run &
BRIDGE_PID=$!

# Wait for the bridge to write the initial dummy-ups state file before starting
# the driver, so upsd has data on the very first read.
echo "[entrypoint] waiting for ${DEV_FILE} ..."
i=0
while [ ! -s "${DEV_FILE}" ]; do
    sleep 1
    i=$((i + 1))
    if ! kill -0 "${BRIDGE_PID}" 2>/dev/null; then
        echo "[entrypoint] bridge exited before producing state file" >&2
        exit 1
    fi
    if [ "${i}" -ge 60 ]; then
        echo "[entrypoint] timed out waiting for state file" >&2
        cleanup
    fi
done

echo "[entrypoint] starting NUT driver + upsd..."
upsdrvctl start
upsd

echo "[entrypoint] up; serving NUT on 3493."

# Supervise: exit (and let Docker restart us) if the bridge dies.
while kill -0 "${BRIDGE_PID}" 2>/dev/null; do
    sleep 5
done

echo "[entrypoint] bridge daemon exited; stopping container." >&2
cleanup
