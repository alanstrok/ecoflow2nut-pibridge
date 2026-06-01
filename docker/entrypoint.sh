#!/bin/sh
# Container entrypoint: run the NUT server (dummy-ups driver + upsd) and the
# EcoFlow BLE bridge daemon together. A simple shell supervisor -- if any
# component exits, we tear everything down so Docker restarts the container.
set -eu

CONFIG="${ECOFLOW_CONFIG:-/app/config/config.yaml}"
NUT_RUN_DIR="/var/run/nut"
DEV_FILE="${NUT_DEV_FILE:-${NUT_RUN_DIR}/ecoflow.dev}"

mkdir -p "${NUT_RUN_DIR}"
chown -R nut:nut "${NUT_RUN_DIR}" 2>/dev/null || true

cleanup() {
    echo "[entrypoint] shutting down..."
    [ -n "${BRIDGE_PID:-}" ] && kill "${BRIDGE_PID}" 2>/dev/null || true
    upsd -c stop 2>/dev/null || true
    upsdrvctl stop 2>/dev/null || true
    exit 0
}
trap cleanup TERM INT

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
