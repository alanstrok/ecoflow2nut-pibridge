#!/usr/bin/env bash
# Bare-metal install helper for Raspberry Pi (Raspberry Pi OS Lite 64-bit).
#
# Installs system dependencies, creates the service user, sets up a virtualenv
# with the bridge package, drops NUT + bridge configuration in place and enables
# the systemd service.
#
# Run from a checkout of this repository:  sudo ./systemd/install.sh
set -euo pipefail

APP_DIR="/opt/ecoflow-nut-bridge"
CONF_DIR="/etc/ecoflow-nut"
NUT_CONF_DIR="/etc/nut"
SERVICE="ecoflow-nut-bridge.service"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ "$(id -u)" -ne 0 ]; then
    echo "Please run as root (sudo)." >&2
    exit 1
fi

echo "==> Installing system packages..."
apt-get update
apt-get install -y --no-install-recommends \
    bluez \
    nut-server \
    nut-client \
    python3 \
    python3-venv \
    python3-pip

echo "==> Creating service user 'ecoflow'..."
if ! id ecoflow >/dev/null 2>&1; then
    useradd --system --home "${APP_DIR}" --shell /usr/sbin/nologin ecoflow
fi
# Allow the service user to access BlueZ and write NUT state.
usermod -aG bluetooth ecoflow || true
usermod -aG nut ecoflow || true

echo "==> Installing application into ${APP_DIR}..."
mkdir -p "${APP_DIR}"
cp -r "${REPO_DIR}/src" "${REPO_DIR}/pyproject.toml" "${REPO_DIR}/README.md" "${APP_DIR}/"
python3 -m venv "${APP_DIR}/.venv"
"${APP_DIR}/.venv/bin/pip" install --upgrade pip
"${APP_DIR}/.venv/bin/pip" install "${APP_DIR}"
chown -R ecoflow:nut "${APP_DIR}"

echo "==> Installing NUT configuration into ${NUT_CONF_DIR}..."
mkdir -p "${NUT_CONF_DIR}"
for f in ups.conf upsd.conf upsd.users upsmon.conf; do
    if [ ! -f "${NUT_CONF_DIR}/${f}" ]; then
        cp "${REPO_DIR}/nut/${f}" "${NUT_CONF_DIR}/${f}"
    else
        echo "    keeping existing ${NUT_CONF_DIR}/${f}"
    fi
done
chown -R root:nut "${NUT_CONF_DIR}"
chmod 640 "${NUT_CONF_DIR}/upsd.users"
# NUT must run in "netserver" mode to serve upsd.
sed -i 's/^MODE=.*/MODE=netserver/' /etc/nut/nut.conf 2>/dev/null || \
    echo "MODE=netserver" > /etc/nut/nut.conf

echo "==> Installing bridge configuration into ${CONF_DIR}..."
mkdir -p "${CONF_DIR}"
if [ ! -f "${CONF_DIR}/config.yaml" ]; then
    cp "${REPO_DIR}/config/config.example.yaml" "${CONF_DIR}/config.yaml"
    echo "    *** edit ${CONF_DIR}/config.yaml with your device MAC/serial ***"
fi
chown -R ecoflow:nut "${CONF_DIR}"

echo "==> Installing systemd unit..."
cp "${REPO_DIR}/systemd/${SERVICE}" "/etc/systemd/system/${SERVICE}"

# Order NUT to start after the bridge, so the bridge's ExecStartPre 'seed' has
# written the dummy-ups state file before the driver tries to read it at boot.
# A drop-in for a unit that does not exist on this system is harmless.
for unit in nut-server.service nut-driver-enumerator.service; do
    dropin="/etc/systemd/system/${unit}.d"
    mkdir -p "${dropin}"
    cat > "${dropin}/ecoflow-bridge.conf" <<EOF
[Unit]
After=${SERVICE}
Wants=${SERVICE}
EOF
done

systemctl daemon-reload
systemctl enable nut-server.service 2>/dev/null || true
systemctl enable "${SERVICE}"

cat <<EOF

==> Done.

Next steps:
  1. Edit ${CONF_DIR}/config.yaml (MAC, serial, user_id; auto_shutdown if wanted).
  2. Edit ${NUT_CONF_DIR}/upsd.users to set real passwords.
  3. Start bridge: sudo systemctl start ${SERVICE}
  4. Start NUT:    sudo systemctl restart nut-server
  5. Verify:       upsc ecoflow@localhost:4141
EOF
